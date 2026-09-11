"""Routes for the mail app, mounted at /api/v1/mail/ by loopd.urls.

One endpoint, and it is the second of only two routes in this project that
something other than our own frontend calls. The path is registered with the
inbound email provider, so its trailing slash is part of the contract:
APPEND_SLASH would answer a slash-less POST with a 301 and the body would be
lost.
"""
from django.urls import path

from mail.webhooks import InboundEmailWebhook

app_name = 'mail'

urlpatterns = [
    path('webhooks/inbound/', InboundEmailWebhook.as_view(), name='inbound-webhook'),
]
