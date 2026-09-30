"""Encrypting the business tokens we hold on our customers' behalf.

A WhatsApp business token lets whoever holds it message every customer of that
business, as that business. We hold one per connected tenant, so this table is
the most valuable thing in the database to steal - and it is in every backup.

So tokens are Fernet-encrypted (AES-128-CBC + HMAC-SHA256, from `cryptography`,
already pinned) with a key that does NOT live in the database.

KEYS
----
`WHATSAPP_TOKEN_KEYS` is a comma-separated list of Fernet keys, newest first.
Encryption always uses the first; decryption tries each - which is what makes
rotation possible: prepend a new key, deploy, re-save the rows, drop the old.

With no key configured a key is derived from SECRET_KEY, so a laptop needs no
setup. whatsapp/checks.py refuses that on a real deployment: SECRET_KEY rotates
for reasons of its own, and rotating it would then silently make every stored
token unreadable.

Pure: settings and `cryptography` only, no models.
"""
import base64
import hashlib

from django.conf import settings


class TokenDecryptError(Exception):
    """A stored token could not be read with any configured key."""


def _fernet():
    from cryptography.fernet import Fernet, MultiFernet

    keys = list(settings.WHATSAPP_TOKEN_KEYS)
    if not keys:
        derived = hashlib.sha256(
            f'whatsapp-token:{settings.SECRET_KEY}'.encode()
        ).digest()
        keys = [base64.urlsafe_b64encode(derived).decode()]
    return MultiFernet([Fernet(key) for key in keys])


def encrypt(plain):
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token):
    if not token:
        return ''
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise TokenDecryptError('Stored WhatsApp token cannot be decrypted') from exc
