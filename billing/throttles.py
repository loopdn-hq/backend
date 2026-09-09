"""Rate limits for endpoints that call out to Stripe.

Scoped throttles (`DEFAULT_THROTTLE_RATES` in settings.py), not
DEFAULT_THROTTLE_CLASSES: only the views that create a real Stripe object
opt in, so `List plans` and `Billing summary` stay unthrottled reads.
"""
from rest_framework.throttling import UserRateThrottle


class CheckoutRateThrottle(UserRateThrottle):
    """Caps how often one user can start a Checkout Session.

    A Checkout Session is a real call to Stripe's API, and each one is a
    fresh row on our side too - nothing stops a buggy retry loop or a
    compromised token from calling this in a loop except this.
    """

    scope = 'billing_checkout'


class BillingPortalRateThrottle(UserRateThrottle):
    scope = 'billing_portal'
