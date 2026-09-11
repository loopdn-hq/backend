"""The endpoint an inbound email provider posts a reply to.

The second public route in this project, and it follows every rule the first
one established - see `sms/webhooks.py`, whose docstring explains the status
codes at length. In short: 200 for every outcome we understand and will not
change our minds about, 4xx for a bad signature and nothing else, 5xx only when
a retry might genuinely help.

WHAT IS VERIFIED, AND WHERE
---------------------------
That the request came from the provider: the signature, here, before the body
is parsed. That the mail came from who it says: the receiving server's SPF,
DKIM and DMARC verdict, read off the message headers by `mail/authentication.py`
and enforced by `mail/inbound.py` beside the check that the From address is the
member's own. Neither is any use without the other - a forged request could
carry a passing verdict, and a genuine request can carry a forged From.

THE EVENT IS ONLY A NOTICE
--------------------------
Resend's inbound event says who wrote to whom and nothing else - no body, no
headers. The content is a second call, made here and only once the address has
passed the one check that needs no network: that it is a reply address we
issued. Everything that knows the provider's shape is in `mail/provider.py`.

The same endpoint receives the RECEIPTS for mail we sent - delivered, bounced,
marked as spam - which `mail/receipts.py` writes into the delivery log. One
route, because the provider offers one webhook; two modules, because posting a
reply and updating a log row have nothing in common but the signature.
"""
import json
import logging

from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from mail import inbound, provider, receipts, signature
from mail.models import EmailWebhookEvent
from mail.outbound import record_inbound
from mail.tokens import participant_id_from

logger = logging.getLogger(__name__)


class ProviderSignatureAuthentication(BaseAuthentication):
    """Proves the request came from the inbound email provider.

    An authentication class rather than a check inside the handler, because DRF
    runs authentication before the body is parsed - so a forged payload is
    rejected without anything ever looking at its contents.
    """

    def authenticate(self, request):
        if not signature.verify(
            request.body,
            request.META.get('HTTP_SVIX_ID', ''),
            request.META.get('HTTP_SVIX_TIMESTAMP', ''),
            request.META.get('HTTP_SVIX_SIGNATURE', ''),
        ):
            raise AuthenticationFailed('Bad signature.')
        return None


class InboundEmailWebhook(APIView):
    """Receives replies members sent to a group's reply address."""

    authentication_classes = [ProviderSignatureAuthentication]
    permission_classes = [AllowAny]
    # The project's envelope renderer is for clients that expect `{code,
    # message, data}`. A webhook sender expects a status code and reads no
    # body, so this answers in plain JSON and keeps its own error contract.
    renderer_classes = [JSONRenderer]

    def post(self, request):
        try:
            payload = json.loads(request.body or b'{}')
        except ValueError:
            # Malformed, and it will still be malformed next time.
            logger.warning('Unparseable inbound email body')
            return Response({'ok': True}, status=status.HTTP_200_OK)

        event_id = _event_id(request, payload)
        event_type = payload.get('type') or ''

        if not event_id:
            return Response({'ok': True}, status=status.HTTP_200_OK)

        # De-duplicate FIRST, before anything with a side effect. A retried
        # event that got as far as posting a reply must not post it twice.
        _, created = EmailWebhookEvent.objects.get_or_create(
            provider_event_id=event_id, defaults={'event_type': event_type[:48]}
        )
        if not created:
            return Response({'ok': True, 'duplicate': True}, status=status.HTTP_200_OK)

        try:
            self._handle(payload)
        except Exception:
            # The dedupe row is REMOVED before answering. It was written as a
            # claim, so two copies of one event arriving together cannot both
            # be processed - but leaving it behind after a failure turns the
            # provider's retry into a no-op and the reply is lost silently.
            EmailWebhookEvent.objects.filter(provider_event_id=event_id).delete()
            logger.exception(
                'Failed handling inbound email %s; asking for a retry', event_id
            )
            return Response(
                {'ok': False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({'ok': True}, status=status.HTTP_200_OK)

    def _handle(self, payload):
        event = provider.parse_event(payload)
        if not event.kind:
            return
        if event.kind == provider.INBOUND:
            self._reply(event)
        else:
            receipts.record(event)

    def _reply(self, event):
        if not event.email_id or not event.to_address:
            return

        if participant_id_from(event.to_address) is None:
            # Not an address we issued - mail to a guessed or mistyped name on
            # the reply domain. Decided from the signature inside the address,
            # before the fetch, so junk sent at the domain costs no API call.
            return

        # A network call inside the request, on purpose. If it fails the
        # exception reaches `post`, which frees the claim and answers 500, and
        # the provider brings the same event back under the same id.
        received = provider.fetch_received(event.email_id)
        from_address = received.from_address or event.from_address

        result = inbound.handle_inbound(
            event.to_address,
            from_address,
            received.body,
            headers=received.headers,
            subject=received.subject,
        )
        if result.participant is None:
            # Nothing to log against. A row keyed to no membership would be a
            # record of an address that mailed us and nothing more.
            return

        record_inbound(
            result.participant.conversation,
            result.participant,
            result.message,
            from_address,
            provider='resend',
        )


def _event_id(request, payload):
    """This delivery's unique id.

    Prefers the transport's id header over anything in the body: it is what the
    sender retries under, which is exactly the identity de-duplication needs.
    """
    header = request.META.get('HTTP_SVIX_ID', '')
    if header:
        return header[:128]
    data = payload.get('data') or {}
    return str(
        data.get('email_id') or data.get('id') or payload.get('id') or ''
    )[:128]
