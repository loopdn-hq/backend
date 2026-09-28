"""Proving a webhook really came from the messaging provider.

Pure: bytes and two header values in, a bool out. No database, no Django models,
so a test can generate a keypair, sign a fixture and assert both acceptance and
every flavour of rejection without any network.

The scheme is Ed25519 over `timestamp.body`, which is what Telnyx signs. The
timestamp is inside the signed material on purpose: without it a captured
request could be replayed verbatim forever, so the signature alone is not
enough - the age is checked too.

`cryptography` is already a pinned dependency, so this needs nothing new.
"""
import base64
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)


def verify(raw_body, timestamp, signature_b64):
    """True when this request is genuinely from the provider and recent.

    With no public keys configured, verification is skipped and everything is
    accepted - development only, and it is deliberately keyed on the ABSENCE of
    keys rather than on a boolean like VERIFY=False. A flag can be inherited by
    a staging deploy and quietly disable authentication on a public endpoint;
    an empty key list is visible in the environment and cannot be set by
    accident.
    """
    public_keys = settings.TELNYX_PUBLIC_KEYS
    if not public_keys:
        logger.warning('Webhook signature not verified: no public keys configured')
        return True

    if not timestamp or not signature_b64:
        return False

    if not _timestamp_is_fresh(timestamp):
        return False

    # Telnyx signs `<timestamp>|<raw body>`. A PIPE, not a dot - signing with
    # the wrong separator produces a signature that never verifies, so every
    # inbound webhook is refused with 403 and the provider retries for ever.
    # Confirmed against a live Telnyx-Signature-Ed25519 header, not the docs.
    signed = timestamp.encode() + b'|' + (raw_body or b'')
    signature = _decode(signature_b64)
    if signature is None:
        return False

    # Every configured key is tried, which is what makes key rotation possible:
    # for a window, both the old and the new key must be accepted.
    for key in public_keys:
        if _verify_with(key, signature, signed):
            return True
    return False


def _timestamp_is_fresh(timestamp):
    try:
        sent_at = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    age = abs(time.time() - sent_at)
    return age <= settings.SMS_WEBHOOK_TOLERANCE_SECONDS


def _decode(value):
    try:
        return base64.b64decode(value)
    except Exception:
        return None


def _verify_with(public_key_b64, signature, signed):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw_key = _decode(public_key_b64)
    if raw_key is None:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, signed)
        return True
    except (InvalidSignature, ValueError):
        return False
