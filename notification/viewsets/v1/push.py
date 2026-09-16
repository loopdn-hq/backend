"""The three things a browser needs from the server.

    GET  /api/v1/push/key/                  the public half of the VAPID pair
    POST /api/v1/push/subscriptions/        remember this browser
    POST /api/v1/push/subscriptions/remove/ forget it

The key is served rather than baked into the web app at build time on purpose:
the frontend is a static export, so a NEXT_PUBLIC_ variable would need a
rebuild and a redeploy to rotate - and would pin the key into every cached
copy of the bundle. Fetching it means the server is the only place it is
configured.
"""
from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from notification.models import PushSubscription
from notification.push.outbound import sending_enabled
from notification.push.serializers import (
    PushSubscriptionRemoveSerializer,
    PushSubscriptionSerializer,
)


class PushKeyView(APIView):
    """The application server key the browser subscribes with.

    Public by nature - it is handed to every browser and travels in every
    subscription - but behind authentication anyway, because nothing in this
    product is anonymous and an open endpoint is one more thing to think about.

    `enabled` says whether the server can actually send. The web app reads it
    to keep the Notifications screen honest: offering a switch that does
    nothing is how "I turned it on and nothing happened" tickets are made.
    """

    permission_classes = [IsAuthenticated]
    success_messages = {'get': 'Push key fetched.'}

    def get(self, request):
        return Response(
            {
                'public_key': settings.VAPID_PUBLIC_KEY,
                'enabled': sending_enabled(),
            }
        )


class PushSubscriptionView(APIView):
    """Remember this browser, so it can be notified."""

    permission_classes = [IsAuthenticated]
    success_messages = {'post': 'Notifications are on for this browser.'}

    def post(self, request):
        serializer = PushSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription = serializer.save(user=request.user)
        return Response(
            {
                'id': subscription.pk,
                'user_agent': subscription.user_agent,
                'created_at': subscription.created_at,
            }
        )


class PushSubscriptionRemoveView(APIView):
    """Forget this browser.

    Scoped to the caller's own rows, so an endpoint somebody else's browser
    registered cannot be unsubscribed by guessing it - the endpoint is a URL,
    and URLs get pasted into places.

    Silent when there is nothing to delete. Turning notifications off has one
    meaning, and "they were already off" is not an error anybody can act on.
    POST rather than DELETE for the same reason `me/blocks/remove` is: it
    carries a body, and this project keeps that shape in one style.
    """

    permission_classes = [IsAuthenticated]
    success_messages = {'post': 'Notifications are off for this browser.'}

    def post(self, request):
        serializer = PushSubscriptionRemoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        removed, _ = PushSubscription.objects.filter(
            user=request.user, endpoint=serializer.validated_data['endpoint']
        ).delete()
        return Response({'removed': bool(removed)})
