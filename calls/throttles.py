"""Rate limits for the calls endpoints that ring people or spend money.

Scoped throttles (`DEFAULT_THROTTLE_RATES` in settings.py), opted into per
action: reading a call, or leaving one, is never limited.
"""
from rest_framework.throttling import UserRateThrottle


class CallStartRateThrottle(UserRateThrottle):
    """Starting a call rings everybody in the conversation."""

    scope = 'call_start'


class CallJoinRateThrottle(UserRateThrottle):
    """Joining mints a token. Cheap, but a client should not need hundreds an hour."""

    scope = 'call_join'


class CallRecordingRateThrottle(UserRateThrottle):
    """Each start is a request to LiveKit's egress service, which is what costs."""

    scope = 'call_recording'
