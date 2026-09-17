"""Proving an inbound email webhook really came from the provider.

Pure: bytes and three header values in, a bool out. No database, no models, so
a test can sign a fixture and assert both acceptance and every flavour of
rejection with no network - the same shape as `sms/signature.py`, which does
this for the phone network.

THE SCHEME
----------
Resend signs its webhooks with Svix, which is HMAC-SHA256 over

    {id}.{timestamp}.{body}

using a secret written `whsec_<base64>`. The header carries one or more
space-separated `v1,<base64 signature>` pairs, several during a secret
rotation, and any one of them matching is enough.

The timestamp is inside the signed material deliberately: without it a captured
request could be replayed verbatim forever, so the age is checked as well as
the signature.
"""
import base64
import hashlib
import hmac
import logging
import time

from django.conf import settings
from django.utils.crypto import constant_time_compare

logger = logging.getLogger(__name__)

SECRET_PREFIX = 'whsec_'


def verify(raw_body, event_id, timestamp, signature_header):
    """True when this request is genuinely from the provider and recent.

    With no secret configured, verification is skipped and everything is
    accepted - development only, and keyed on the ABSENCE of a secret rather
    than on a boolean like VERIFY=False. A flag can be inherited by a staging
    deploy and quietly disable authentication on a public endpoint; an empty
    secret is visible in the environment and cannot be set by accident.
    """
    secret = getattr(settings, 'RESEND_WEBHOOK_SECRET', '')
    if not secret:
        logger.warning(
            'Inbound email signature not verified: no RESEND_WEBHOOK_SECRET set'
        )
        return True

    if not event_id or not timestamp or not signature_header:
        return False

    if not _timestamp_is_fresh(timestamp):
        return False

    key = _decode_secret(secret)
    if key is None:
        return False

    signed = f'{event_id}.{timestamp}.'.encode() + (raw_body or b'')
    expected = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()

    # Every version pair is tried, which is what makes rotation possible: for a
    # window both the old and the new secret sign the same request.
    for part in signature_header.split():
        _, _, candidate = part.partition(',')
        if candidate and constant_time_compare(candidate, expected):
            return True
    return False


def _timestamp_is_fresh(timestamp):
    try:
        sent_at = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    age = abs(time.time() - sent_at)
    return age <= settings.MAIL_WEBHOOK_TOLERANCE_SECONDS


def _decode_secret(secret):
    """The raw HMAC key out of a `whsec_...` secret."""
    body = secret[len(SECRET_PREFIX):] if secret.startswith(SECRET_PREFIX) else secret
    try:
        return base64.b64decode(body)
    except Exception:
        logger.error('RESEND_WEBHOOK_SECRET is not valid base64')
        return None
