"""The endpoint an inbound email provider posts a reply to.

The second public route in this project, and it follows every rule the first
one established - see `sms/webhooks.py`, whose docstring explains the status
codes at length. In short: 200 for every outcome we understand and will not
change our minds about, 4xx for a bad signature and nothing else, 5xx only when
a retry might genuinely help.

WHAT IS NOT VERIFIED HERE
-------------------------
That the sending domain authorised this mail. SPF, DKIM and DMARC are checked
by the inbound provider before it ever calls this, which is the entire reason
`mail/inbound.py` is allowed to trust the From address - and the reason an
inbound provider is worth having rather than running an SMTP listener.

THE PAYLOAD SHAPE IS THE PROVIDER'S
-----------------------------------
`_extract` is the only function here that knows what an inbound event looks
like, so a change of provider is a change to one function. It reads
defensively, because it has been written against documentation rather than
against a live account: an event that does not carry what it needs is answered
200 and dropped, not retried forever.
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

from mail import inbound, signature
from mail.models import EmailWebhookEvent
from mail.outbound import record_inbound

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
        to_address, from_address, body = _extract(payload)
        if not to_address:
            return

        result = inbound.handle_inbound(to_address, from_address, body)
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
    return str(data.get('id') or payload.get('id') or '')[:128]


def _extract(payload):
    """(to, from, text) out of one provider event. The only provider-shaped code.

    Returns an empty `to` for anything that is not an inbound message - a
    delivery receipt, a bounce, an event type added after this was written -
    which the caller treats as nothing to do.
    """
    if (payload.get('type') or '') not in ('email.received', 'inbound.email', ''):
        return '', '', ''

    data = payload.get('data') or payload
    to_address = _first(data.get('to'))
    from_address = _first(data.get('from'))
    text = data.get('text') or ''

    if not text and data.get('html'):
        # Some clients send HTML only. Better a rough plain-text rendering than
        # posting markup into a group, and better still than dropping it.
        text = _strip_html(data['html'])

    return to_address, from_address, text


def _first(value):
    """One address out of a field that may be a string or a list of them."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ''
    if isinstance(value, dict):
        # Some providers wrap each address as {'address': ..., 'name': ...}.
        value = value.get('address') or value.get('email') or ''
    return str(value or '').strip()


def _strip_html(html):
    """A crude plain-text rendering of an HTML-only reply.

    Crude on purpose. The alternative is an HTML parser deciding what a group
    message says, and the text alternative is present on the overwhelming
    majority of real mail - this is the fallback, not the path.
    """
    import re
    from html import unescape

    text = re.sub(r'(?is)<(script|style).*?</\1>', ' ', html)
    text = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</tr>', '\n', text)
    text = re.sub(r'(?s)<[^>]+>', '', text)
    return unescape(text).strip()
