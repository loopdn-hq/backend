"""Routes for the calls app, mounted at /api/v1/ by loopd.urls.

Mounted one level up from the app's own name because it owns two sibling
resources - /api/v1/calls/ and /api/v1/recordings/ - and the second is not
under the first.
"""
from django.urls import path
from rest_framework.routers import SimpleRouter

from calls.viewsets.v1.calls import CallViewSet
from calls.viewsets.v1.recordings import RecordingViewSet
from calls.webhooks import LiveKitWebhook

app_name = 'calls'

router = SimpleRouter()
router.register('calls', CallViewSet, basename='call')
router.register('recordings', RecordingViewSet, basename='recording')

urlpatterns = [
    # Ahead of the router so `webhooks` can never be read as a call id.
    path('calls/webhooks/livekit/', LiveKitWebhook.as_view(), name='livekit-webhook'),
    *router.urls,
]
