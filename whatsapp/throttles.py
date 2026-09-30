"""Rate limits for the WhatsApp endpoints that reach Meta or message people."""
from rest_framework.throttling import UserRateThrottle


class WhatsAppConnectThrottle(UserRateThrottle):
    """Each connect is several Graph API calls on the customer's account."""

    scope = 'whatsapp_connect'


class WhatsAppInviteThrottle(UserRateThrottle):
    """Each invitation is a paid template message to a stranger's phone."""

    scope = 'whatsapp_invite'
