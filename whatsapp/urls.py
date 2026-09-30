"""Routes for the whatsapp app, mounted at /api/v1/whatsapp/ by loopd.urls.

The webhook's trailing slash is part of the contract with Meta, as for SMS:
APPEND_SLASH would answer a slash-less POST with a 301 and lose the body.
"""
from django.urls import path
from rest_framework.routers import SimpleRouter

from whatsapp.views import (
    ConfigView,
    ConnectionViewSet,
    GroupConsentView,
    GroupInviteView,
    GroupWhatsAppView,
)
from whatsapp.webhooks import WhatsAppWebhook

app_name = 'whatsapp'

router = SimpleRouter()
router.register('connections', ConnectionViewSet, basename='connection')

urlpatterns = [
    path('webhooks/meta/', WhatsAppWebhook.as_view(), name='webhook'),
    path('config/', ConfigView.as_view(), name='config'),
    path('groups/<int:conversation_id>/', GroupWhatsAppView.as_view(), name='group'),
    path('groups/<int:conversation_id>/consent/', GroupConsentView.as_view(),
         name='group-consent'),
    path('groups/<int:conversation_id>/invite/', GroupInviteView.as_view(),
         name='group-invite'),
    *router.urls,
]
