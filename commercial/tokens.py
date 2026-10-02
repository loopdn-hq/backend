"""The two tokens a served card carries: one to say it was seen, one behind
its button.

docs/campaigns_final_plan.md, sections 10 and 11. Serving writes nothing to
the database; it signs what it served instead - which campaign and creative,
in which group, to whom, where on screen, and a random nonce - and the chat
app hands it back. Signed, not stored, like mail/tokens.py.

DELIVERY: reported when the card has been on screen (slice 5). It expires
(SPONSORED_TOKEN_MAX_AGE_SECONDS), because a view is only a view while the
card is fresh. The nonce is AdImpression.delivery_token, unique, so one serve
is one view at most.

CLICK: in the button's address, /c/{token}/ (slice 6). The same facts and the
same nonce, under another salt, so neither can be passed off as the other. A
stale click token still names its destination - the button keeps working -
but the click is not counted (SPONSORED_CLICK_TOKEN_MAX_AGE_SECONDS).

Nothing in either is trusted beyond "this server served this": measurement
still checks the viewer is still in the group.
"""
import secrets

from django.conf import settings
from django.core import signing

DELIVERY_SALT = 'commercial.delivery'
CLICK_SALT = 'commercial.click'


def new_nonce():
    return secrets.token_hex(16)


def issue_delivery(*, nonce, **facts):
    """The token for one served card's view. `facts` as `_pack` takes them."""
    return signing.dumps(_pack(nonce, **facts), salt=DELIVERY_SALT, compress=True)


def issue_click(*, nonce, **facts):
    """The token in one served card's button address."""
    return signing.dumps(_pack(nonce, **facts), salt=CLICK_SALT, compress=True)


def read_delivery(token):
    """What a delivery token says, or None for a forged, mangled or expired one.

    {"campaign_id", "creative_id", "conversation_id", "viewer_id", "placement",
    "nonce"}. One answer for every kind of wrong, so the difference is no
    oracle.
    """
    data, fresh = _read(token, DELIVERY_SALT, settings.SPONSORED_TOKEN_MAX_AGE_SECONDS)
    return data if fresh else None


def read_click(token):
    """(facts, fresh) for a click token: (None, False) when it is not ours, and
    fresh=False when it is ours but too old to count."""
    return _read(token, CLICK_SALT, settings.SPONSORED_CLICK_TOKEN_MAX_AGE_SECONDS)


def _pack(nonce, *, campaign_id, creative_id, conversation_id, viewer_id, placement):
    return {
        'c': campaign_id,
        'k': creative_id,
        'g': conversation_id,
        'v': viewer_id,
        'p': placement,
        'n': nonce,
    }


def _read(token, salt, max_age):
    if not isinstance(token, str) or not token or len(token) > 500:
        return None, False
    fresh = True
    try:
        data = signing.loads(token, salt=salt, max_age=max_age)
    except signing.SignatureExpired:
        fresh = False
        try:
            data = signing.loads(token, salt=salt)
        except signing.BadSignature:
            return None, False
    except signing.BadSignature:
        return None, False
    try:
        return {
            'campaign_id': int(data['c']),
            'creative_id': int(data['k']),
            'conversation_id': int(data['g']),
            'viewer_id': int(data['v']),
            'placement': str(data['p']),
            'nonce': str(data['n']),
        }, fresh
    except (KeyError, TypeError, ValueError):
        return None, False
