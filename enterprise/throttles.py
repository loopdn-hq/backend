from rest_framework.throttling import UserRateThrottle


class EnterpriseCheckoutRateThrottle(UserRateThrottle):
    """Caps how often one account can open an Enterprise Checkout Session -
    same reasoning as billing.throttles.CheckoutRateThrottle."""

    scope = 'enterprise_checkout'
