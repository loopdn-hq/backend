"""The one public endpoint a phone network can reach.

Everything else in this project is authenticated with a token belonging to a
person. This is not: the caller is a machine, the payload arrives unsolicited,
and the only proof of who sent it is a signature over the bytes.

STATUS CODES ARE THE CONTRACT
-----------------------------
A provider reads any non-2xx as "that did not arrive, send it again" and retries
- sometimes for hours. So the codes here are chosen for what they make the
provider DO, not for how they read:

  200  every outcome we understand and will not change our minds about:
       duplicate event, unknown number, unknown sender, blocked sender,
       malformed payload. None of these improve on a retry, and answering
       anything else turns one bad message into a loop.
  4xx  a bad signature, and nothing else. Never retried, nothing leaked.
  5xx  "we could not deal with this, please do resend" - an unexpected failure
       while handling a well-formed event. The de-duplication row is deleted
       first, so the retry can actually reprocess it rather than being waved
       through as a duplicate.

That is also why this view never lets an exception reach the project's normal
handler: an IntegrityError there becomes a 409, which a provider treats as
failure, and the duplicate protection would turn into the retry loop it exists
to prevent.

ORDER
-----
Verify, then de-duplicate, then act. The signature check runs as authentication,
before DRF parses anything, so unsigned bytes never reach a parser.
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from sms import inbound, signature
from sms.models import GroupNumber, SmsDelivery, SmsWebhookEvent

logger = logging.getLogger(__name__)


class ProviderSignatureAuthentication(BaseAuthentication):
    """Proves the request came from the messaging provider.

    An authentication class rather than a check inside the handler, because DRF
    runs authentication before the body is parsed - so a forged payload is
    rejected without anything ever looking at its contents.
    """

    def authenticate(self, request):
        raw = request.body
        if not signature.verify(
            raw,
            request.META.get('HTTP_TELNYX_TIMESTAMP', ''),
            request.META.get('HTTP_TELNYX_SIGNATURE_ED25519', ''),
        ):
            raise AuthenticationFailed('Bad signature.')
        return None


class InboundSmsWebhook(APIView):
    """Receives everything the provider has to tell us about messages."""

    authentication_classes = [ProviderSignatureAuthentication]
    permission_classes = [AllowAny]
    # The project's envelope renderer is for clients that expect `{code,
    # message, data}`. A phone network expects a status code and reads no body,
    # so this answers in plain JSON and keeps its own error contract.
    renderer_classes = [JSONRenderer]

    def post(self, request):
        try:
            payload = json.loads(request.body or b'{}')
        except ValueError:
            # Malformed and will still be malformed next time.
            logger.warning('Unparseable webhook body')
            return Response({'ok': True}, status=status.HTTP_200_OK)

        data = payload.get('data') or {}
        event_id = data.get('id') or ''
        event_type = data.get('event_type') or ''

        if not event_id:
            return Response({'ok': True}, status=status.HTTP_200_OK)

        # De-duplicate FIRST, before anything with a side effect. A retried
        # event that got as far as posting a message must not post it twice.
        _, created = SmsWebhookEvent.objects.get_or_create(
            provider_event_id=event_id, defaults={'event_type': event_type}
        )
        if not created:
            return Response({'ok': True, 'duplicate': True}, status=status.HTTP_200_OK)

        try:
            self._handle(event_type, data.get('payload') or {})
        except Exception:
            # The dedupe row is REMOVED before answering. It was written first
            # as a claim, so that two copies of the same event arriving at once
            # cannot both be processed - but leaving it behind after a failure
            # turns the provider's retry into a no-op and the message is simply
            # lost, silently, forever.
            #
            # Deleting it and answering 5xx is what makes the retry work. A
            # permanently failing event stops when the provider gives up
            # retrying, which is the right place for that decision.
            SmsWebhookEvent.objects.filter(provider_event_id=event_id).delete()
            logger.exception('Failed handling webhook event %s; asking for a retry',
                             event_id)
            return Response(
                {'ok': False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({'ok': True}, status=status.HTTP_200_OK)

    # -- the two things a provider tells us ---------------------------------

    def _handle(self, event_type, payload):
        if event_type == 'message.received':
            return self._received(payload)
        return self._status_update(payload)

    def _received(self, payload):
        to_number = _first_to(payload)
        from_number = (payload.get('from') or {}).get('phone_number', '')
        text = payload.get('text') or ''

        group_number = GroupNumber.for_number(to_number)
        if group_number is None:
            # Nobody's group. Silence, and no reply - answering tells whoever
            # is probing that the number is live.
            logger.info('Inbound SMS for an unrouted number ...%s', to_number[-4:])
            return

        result = inbound.handle_inbound(
            group_number.conversation, from_number, text
        )

        # Every text to a group's number, not only the ones that became a
        # message: JOIN, STOP and HELP are billed exactly like "dinner at 7",
        # and were missing from the record that is supposed to add up to the
        # bill.
        from sms.outbound import record_inbound

        record_inbound(
            group_number.conversation,
            result.participant,
            result.message,
            from_number,
            provider=group_number.provider,
            text=text,
            provider_message_id=payload.get('id') or '',
            cost=_cost(payload),
        )

        if result.reply:
            self._reply(group_number, from_number, result.reply)

    def _status_update(self, payload):
        """A message we sent has moved on: sent, delivered, or failed."""
        message_id = payload.get('id') or ''
        if not message_id:
            return

        to_entries = payload.get('to') or []
        provider_status = (to_entries[0] or {}).get('status', '') if to_entries else ''

        mapping = {
            'sent': SmsDelivery.Status.SENT,
            'delivered': SmsDelivery.Status.DELIVERED,
            'delivery_failed': SmsDelivery.Status.FAILED,
            'failed': SmsDelivery.Status.FAILED,
            # Refused before it left the provider - a 10DLC block arrives like
            # this - and gave up on. Both were read as nothing at all, which
            # left a text that never went anywhere marked "sent".
            'sending_failed': SmsDelivery.Status.FAILED,
            'expired': SmsDelivery.Status.FAILED,
        }
        new_status = mapping.get(provider_status)

        fields = {}
        if new_status is not None:
            fields['status'] = new_status
        if new_status == SmsDelivery.Status.DELIVERED:
            fields['delivered_at'] = timezone.now()
        parts = payload.get('parts')
        if isinstance(parts, int) and parts > 0:
            # The provider's count is authoritative; ours was an estimate.
            fields['segments'] = parts

        # Read whatever the status: the price can ride an event whose status
        # says nothing new, and the last event to carry one is the final word.
        cost = _cost(payload)
        if cost is not None:
            fields['cost'], fields['cost_currency'] = cost
        if new_status == SmsDelivery.Status.FAILED:
            code, detail = _error(payload)
            if code or detail:
                fields['error_code'] = code
                fields['error_detail'] = detail

        if not fields:
            return
        SmsDelivery.objects.filter(provider_message_id=message_id).update(**fields)

    def _reply(self, group_number, to_number, text):
        """Answer the sender, within a cap.

        Replies are billed messages triggered by an unauthenticated inbound
        text, which is the classic way to make somebody else pay: text HELP in
        a loop and every one costs us. So they are rate limited per number, and
        a failure to reply is never allowed to fail the webhook.
        """
        from sms.outbound import send_reply

        try:
            send_reply(group_number.conversation, group_number, to_number, text)
        except Exception:
            # A reply that cannot be sent is never allowed to fail the webhook -
            # the inbound message is already stored, and a non-2xx would make
            # the provider send the whole thing again.
            logger.exception('Could not reply to ...%s', to_number[-4:])


def _cost(payload):
    """(amount, currency) from a provider payload, or None when it gave none.

    Telnyx sends `cost: {"amount": "0.0051", "currency": "USD"}` - the amount a
    STRING, so it is read straight into a Decimal and never through a float.
    Null, missing or unreadable is None: "not priced", never zero.
    """
    cost = payload.get('cost')
    if not isinstance(cost, dict):
        return None
    try:
        amount = Decimal(str(cost.get('amount')))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    return amount, str(cost.get('currency') or '')[:3].upper()


def _error(payload):
    """(code, detail) of the first error a provider payload carries.

    Telnyx puts them in `errors: [{"code", "title", "detail"}]`; some events
    carry them on the recipient (`to[0].errors`) instead, so both are read.
    The title is the short reason ("Not 10DLC registered"); the detail is used
    only when there is no title.
    """
    errors = payload.get('errors')
    if not errors:
        to_entries = payload.get('to') or []
        first = to_entries[0] if to_entries and isinstance(to_entries[0], dict) else {}
        errors = first.get('errors')
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return '', ''
    error = errors[0]
    return (
        str(error.get('code') or '')[:16],
        str(error.get('title') or error.get('detail') or '')[:255],
    )


def _first_to(payload):
    """The number the text was sent TO.

    Providers vary: some send a string, some a list of destinations with
    per-recipient status. Both shapes are handled because getting it wrong
    means every inbound message routes nowhere.
    """
    to = payload.get('to')
    if isinstance(to, str):
        return to
    if isinstance(to, list) and to:
        first = to[0]
        if isinstance(first, dict):
            return first.get('phone_number', '')
        return str(first)
    return ''
