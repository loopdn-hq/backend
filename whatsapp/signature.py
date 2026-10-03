"""Proving a webhook really came from Meta.

Meta signs each POST with HMAC-SHA256 over the raw body, keyed with our app
secret, and sends it as `X-Hub-Signature-256: sha256=<hex>`.

With no app secret configured, verification is skipped - development only, and
keyed on the ABSENCE of the secret rather than on a flag, for the reason
sms/signature.py gives. whatsapp/checks.py refuses to deploy that way.

Pure: bytes and a header in, a bool out.
"""
import hashlib
import hmac
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def verify(raw_body, header):
    secret = settings.WHATSAPP_APP_SECRET
    if not secret:
        logger.warning('WhatsApp webhook signature not verified: no app secret configured')
        return True

    if not header or not header.startswith('sha256='):
        return False

    expected = hmac.new(secret.encode(), raw_body or b'', hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len('sha256='):].strip().lower())
