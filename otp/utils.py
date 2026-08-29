"""Helpers for building one-time passcodes."""
import secrets

from django.conf import settings
from django.utils import timezone


def generate_code(length=None):
    """Return a numeric one-time passcode of `length` digits.

    Uses `secrets`, not `random`: the latter is a Mersenne Twister, and
    observing a handful of its outputs is enough to predict every code it
    will produce afterwards.

    Built digit by digit so that a leading zero survives - `randint(0,
    999999)` would return '42' about one time in a million.
    """
    length = length or settings.OTP_CODE_LENGTH
    return ''.join(str(secrets.randbelow(10)) for _ in range(length))


def default_expiry():
    """When a code issued right now should stop working."""
    return timezone.now() + settings.OTP_TTL
