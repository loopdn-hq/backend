"""The one address Meta calls, for every connected business number.

One webhook for the whole app, not one per tenant: Meta delivers every WABA
that subscribed to our app here, and `metadata.phone_number_id` says which
connection an item belongs to.

    GET   the verification handshake Meta performs when the URL is saved in the
          app dashboard: echo `hub.challenge` if `hub.verify_token` matches.
    POST  signed with X-Hub-Signature-256 (whatsapp/signature.py).

STATUS CODES, as in sms/webhooks.py: 200 for everything a retry cannot improve
(unknown number, duplicate, malformed item), 401 for a bad signature, and 500
only when handling a well-formed item failed unexpectedly - with that item's
dedupe claim released first, so Meta's retry actually reprocesses it.
"""
import json
import logging

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from whatsapp import inbound, signature
from whatsapp.models import WhatsAppConnection, WhatsAppDelivery, WhatsAppWebhookEvent

logger = logging.getLogger(__name__)


class MetaSignatureAuthentication(BaseAuthentication):
    def authenticate(self, request):
        if request.method != 'POST':
            return None
        if not signature.verify(request.body, request.META.get('HTTP_X_HUB_SIGNATURE_256', '')):
            raise AuthenticationFailed('Bad signature.')
        return None

    def authenticate_header(self, request):
        # Makes DRF answer 401 rather than 403, which is what a bad signature is.
        return 'X-Hub-Signature-256'


class WhatsAppWebhook(APIView):
    authentication_classes = [MetaSignatureAuthentication]
    permission_classes = [AllowAny]
    renderer_classes = [JSONRenderer]
    parser_classes = []

    def get(self, request):
        params = request.query_params
        expected = settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
        if (
            expected
            and params.get('hub.mode') == 'subscribe'
            and params.get('hub.verify_token') == expected
        ):
            # Plain text, not JSON: Meta compares the body byte for byte.
            return HttpResponse(params.get('hub.challenge', ''), content_type='text/plain')
        return HttpResponse(status=403)

    def post(self, request):
        try:
            payload = json.loads(request.body or b'{}')
        except ValueError:
            logger.warning('Unparseable WhatsApp webhook body')
            return Response({'ok': True})

        failed = False
        for entry in payload.get('entry') or []:
            for change in entry.get('changes') or []:
                field = change.get('field')
                value = change.get('value') or {}
                if field == 'messages':
                    failed |= self._messages(value)
                elif field == 'account_update':
                    self._account_update(entry.get('id'), value)
                elif field == 'phone_number_quality_update':
                    self._quality_update(entry.get('id'), value)

        if failed:
            return Response({'ok': False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({'ok': True})

    # -- messages -------------------------------------------------------------

    def _messages(self, value):
        """Every message and status in one change. True if any item failed."""
        phone_number_id = (value.get('metadata') or {}).get('phone_number_id', '')
        connection = WhatsAppConnection.for_phone_number_id(phone_number_id)
        if connection is None:
            logger.info('WhatsApp webhook for an unconnected number %s', phone_number_id)
            return False

        names = {
            c.get('wa_id'): (c.get('profile') or {}).get('name', '')
            for c in value.get('contacts') or []
        }

        failed = False
        for item in value.get('messages') or []:
            failed |= self._once(
                f'msg:{item.get("id")}', lambda item=item: self._message(connection, item, names)
            )
        for item in value.get('statuses') or []:
            failed |= self._once(
                f'status:{item.get("id")}:{item.get("status")}',
                lambda item=item: self._status(item),
            )
        return failed

    @staticmethod
    def _once(key, handler):
        """Run `handler` once per key, ever. True if it failed and should be retried."""
        if key.endswith(':None') or key.startswith('msg:None'):
            return False
        _, created = WhatsAppWebhookEvent.objects.get_or_create(key=key[:200])
        if not created:
            return False
        try:
            handler()
        except Exception:
            WhatsAppWebhookEvent.objects.filter(key=key[:200]).delete()
            logger.exception('Failed handling WhatsApp webhook item %s; asking for a retry', key)
            return True
        return False

    def _message(self, connection, item, names):
        from whatsapp.outbound import record_inbound, send_reply

        wa_id = item.get('from') or ''
        text = _text_of(item)
        if text is None:
            # Media, location, a reaction... Text only for now. Tell them,
            # since they wrote in good faith and silence looks broken.
            # Strangers still get silence, as for text.
            from whatsapp.routing import memberships

            inbound.touch_contact(connection, wa_id, names.get(wa_id, ''))
            if (
                item.get('type') not in ('reaction', 'system', 'unsupported')
                and memberships(connection, wa_id)
            ):
                send_reply(connection, wa_id,
                           'Only text messages can be posted to the group for now.')
            return

        result = inbound.handle_inbound(
            connection, wa_id, text,
            profile_name=names.get(wa_id, ''),
            context_wamid=(item.get('context') or {}).get('id', ''),
        )

        if result.message is not None:
            record_inbound(connection, result.message.conversation, result.participant,
                           result.message, wa_id, item.get('id', ''))
        if result.reply:
            send_reply(connection, wa_id, result.reply, reply_to=item.get('id'))

    @staticmethod
    def _status(item):
        mapping = {
            'sent': WhatsAppDelivery.Status.SENT,
            'delivered': WhatsAppDelivery.Status.DELIVERED,
            'read': WhatsAppDelivery.Status.READ,
            'failed': WhatsAppDelivery.Status.FAILED,
        }
        new_status = mapping.get(item.get('status'))
        if new_status is None or not item.get('id'):
            return

        rows = WhatsAppDelivery.objects.filter(
            wamid=item['id'], direction=WhatsAppDelivery.Direction.OUTBOUND
        )
        # Statuses can arrive out of order; `sent` after `read` must not undo it.
        order = ['sent', 'delivered', 'read']
        if new_status in order:
            rows = rows.exclude(status__in=order[order.index(new_status) + 1:])

        fields = {'status': new_status, 'updated_at': timezone.now()}
        if new_status == WhatsAppDelivery.Status.DELIVERED:
            fields['delivered_at'] = timezone.now()
        if new_status == WhatsAppDelivery.Status.READ:
            fields['read_at'] = timezone.now()
        if new_status == WhatsAppDelivery.Status.FAILED:
            error = (item.get('errors') or [{}])[0]
            fields['reason'] = f'{error.get("title", "failed")} ({error.get("code", "")})'[:160]
        category = (item.get('pricing') or {}).get('category')
        if category:
            fields['pricing_category'] = str(category)[:32]
        rows.update(**fields)

    # -- account events -------------------------------------------------------

    @staticmethod
    def _account_update(waba_id, value):
        """The owner removed our app in Business Manager, or Meta restricted them."""
        event = value.get('event')
        if event not in ('PARTNER_REMOVED', 'ACCOUNT_DELETED', 'DISABLED_UPDATE'):
            return
        from whatsapp.onboarding import disconnect

        for connection in WhatsAppConnection.objects.filter(
            waba_id=str(waba_id), status=WhatsAppConnection.Status.ACTIVE
        ):
            disconnect(connection, reason='removed_by_meta' if event == 'PARTNER_REMOVED'
                       else f'meta: {event}')

    @staticmethod
    def _quality_update(waba_id, value):
        # Messaging limits are shared by every number in a business portfolio,
        # so the whole WABA takes the new tier.
        limit = str(value.get('current_limit', ''))[:32]
        if waba_id and limit:
            WhatsAppConnection.objects.filter(
                waba_id=str(waba_id), status=WhatsAppConnection.Status.ACTIVE,
            ).update(messaging_limit_tier=limit, updated_at=timezone.now())


def _text_of(item):
    """The text of a message, or None for a kind we cannot post."""
    kind = item.get('type')
    if kind == 'text':
        return (item.get('text') or {}).get('body', '')
    if kind == 'button':
        # A quick-reply button on a template, e.g. the invitation's "Join".
        button = item.get('button') or {}
        return button.get('payload') or button.get('text') or ''
    if kind == 'interactive':
        reply = (item.get('interactive') or {})
        chosen = reply.get('button_reply') or reply.get('list_reply') or {}
        return chosen.get('id') or chosen.get('title') or ''
    # Media is not posted yet - not even its caption, which would put words in
    # the group that referred to a photo nobody can see.
    return None
