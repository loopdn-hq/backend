"""The address LiveKit calls to tell us what happened in a room.

Public, like the Stripe and Telnyx webhooks, and authenticated the same way they
are: by signature, not by a token. LiveKit signs each delivery with the API key
and secret (calls/livekit.py:parse_webhook), so a request that does not verify
is refused before anything is read from it.

What it is for: Django cannot see a connection. A participant who closes their
laptop never tells us, a recording finishes long after the request that started
it, and a room LiveKit closes for being empty is only known here through this.
"""
import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from calls import services
from calls.livekit import CallServiceError, LiveKitNotConfigured, parse_webhook

logger = logging.getLogger(__name__)


class LiveKitWebhook(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    # LiveKit reads a status code and nothing else, so this answers in plain
    # JSON rather than the project's envelope - as billing/webhooks.py does.
    renderer_classes = [JSONRenderer]
    # Its Content-Type is `application/webhook+json`, which no DRF parser
    # accepts, so the body is read raw below and never parsed by the framework.
    parser_classes = []

    def post(self, request):
        try:
            event = parse_webhook(
                request.body.decode('utf-8', errors='replace'),
                request.META.get('HTTP_AUTHORIZATION', ''),
            )
        except LiveKitNotConfigured:
            return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except CallServiceError:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        try:
            services.handle_webhook(event)
        except Exception:
            # 500, so LiveKit retries: a delivery we failed to act on is one we
            # would rather see again than lose.
            logger.exception('Could not handle LiveKit webhook %s', event.event)
            return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(status=status.HTTP_200_OK)
