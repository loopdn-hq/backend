"""The delivery token: what a served card carries back when it is seen.

docs/campaigns_final_plan.md, section 10. Serving writes nothing to the
database; it signs what it served instead - which campaign and creative, in
which group, to whom, where on screen, and a random nonce - and the chat app
hands the string back when the card has been on screen. Signed, not stored,
like mail/tokens.py; unlike those, it expires
(SPONSORED_TOKEN_MAX_AGE_SECONDS), because a view is only a view while the
card is fresh.

Its own salt, so no other signed string in the app can be passed off as one.
The click token (slice 6) gets another.

The nonce is what makes one serve one view at most: it is
AdImpression.delivery_token, unique. Nothing in the token is trusted beyond
"this server served this": measurement still checks the viewer is the one
reporting and is still in the group.
"""
import secrets

from django.conf import settings
from django.core import signing

DELIVERY_SALT = 'commercial.delivery'


def issue_delivery(*, campaign_id, creative_id, conversation_id, viewer_id, placement):
    """The token for one served card."""
    return signing.dumps(
        {
            'c': campaign_id,
            'k': creative_id,
            'g': conversation_id,
            'v': viewer_id,
            'p': placement,
            'n': secrets.token_hex(16),
        },
        salt=DELIVERY_SALT,
        compress=True,
    )


def read_delivery(token):
    """What a delivery token says, or None for a forged, mangled or expired one.

    {"campaign_id", "creative_id", "conversation_id", "viewer_id", "placement",
    "nonce"}. One answer for every kind of wrong, so the difference is no
    oracle.
    """
    if not isinstance(token, str) or not token or len(token) > 500:
        return None
    try:
        data = signing.loads(
            token, salt=DELIVERY_SALT, max_age=settings.SPONSORED_TOKEN_MAX_AGE_SECONDS
        )
    except signing.BadSignature:
        return None
    try:
        return {
            'campaign_id': int(data['c']),
            'creative_id': int(data['k']),
            'conversation_id': int(data['g']),
            'viewer_id': int(data['v']),
            'placement': str(data['p']),
            'nonce': str(data['n']),
        }
    except (KeyError, TypeError, ValueError):
        return None
