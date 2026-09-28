"""Per-IP ceilings on the password reset endpoints.

These sit ALONGSIDE the per-email limits in `user/serializers.py`, and the two
answer different questions. The per-email limit stops one address being spammed
with reset links; it does nothing about somebody walking a list of addresses,
because every address gets its own budget. That is what these bound.

Both scopes are anonymous by nature - a person resetting a password is signed
out - so `AnonRateThrottle` keys them on the client IP and stores the counters
in the default cache, which is Redis in production and therefore shared across
replicas. On a laptop it is LocMemCache and per-process, the same trade-off
every other cache-backed limit here makes.

The rates are deliberately loose enough for a shared IP - an office, a
household behind one NAT - because the per-email limit is the tight one.
"""
from rest_framework.throttling import AnonRateThrottle


class PasswordResetRequestThrottle(AnonRateThrottle):
    """POST forgot-password. The enumeration ceiling.

    Probing whether an address is registered costs one request each, so this
    is what makes walking a list expensive no matter what the response body
    does or does not reveal.
    """

    scope = 'password_reset_request'


class PasswordResetConfirmThrottle(AnonRateThrottle):
    """POST reset-password. The brute-force ceiling.

    Guessing a token is not realistic - it is an HMAC over the password hash,
    keyed on SECRET_KEY - so this is a bound on an unauthenticated endpoint
    rather than a defence against a plausible attack. Set high enough that a
    person who keeps picking passwords the validators reject, each of which
    costs a request, does not lock themselves out.
    """

    scope = 'password_reset_confirm'
