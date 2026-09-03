"""Routes for the sms app, mounted at /api/v1/sms/ by loopd.urls.

One endpoint, and it is the only route in the project a phone network calls.
The path is registered with the provider, so its trailing slash is part of the
contract: APPEND_SLASH would answer a slash-less POST with a 301 and the body
would be lost.
"""
from django.urls import path

from sms.webhooks import InboundSmsWebhook

app_name = 'sms'

urlpatterns = [
    path('webhooks/inbound/', InboundSmsWebhook.as_view(), name='inbound-webhook'),
]
