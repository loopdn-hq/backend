"""The two opaque strings a group's email carries, and reading them back.

    reply+312.4f1c9a0b83d7e6512aa4@reply.loopdn.com
    https://api.loopdn.com/mail/unsubscribe/312.9c1d0e77af23b5610cc8/

Both name ONE membership - one person, in one group. That is the only shape
that is safe: anything built from the conversation alone (`group-123@...`) is a
public handle to a private group, and whoever is forwarded a single message
holds it forever.

TWO SCHEMES, ONE MODULE, DIFFERENT SALTS
----------------------------------------
They live together so the rule between them stays visible, and the rule is that
their salts must never be shared.

The reply address is printed in the `Reply-To` of every group email and travels
in every forward of it. Under a shared salt, anybody holding
`reply+312.<tag>@...` could paste `<tag>` into an unsubscribe URL for member 312
and the other way round - one string would be two permissions. Distinct salts
make them structurally non-interchangeable, and a test asserts it.

The unsubscribe token signs `<id>.email` rather than the bare id, so the voice
channel's token will be non-interchangeable with this one for free, without
inventing a third salt.

SIGNED, NOT STORED
------------------
There is no token table. Each token is an id with an HMAC of it alongside, so
the string is derived on the way out and verified on the way back with nothing
to create, expire or clean up.

Nothing about either token is trusted. It says WHICH membership to look at, and
the caller then re-checks everything that matters - `mail.inbound` that the
membership is live and the sender is who they claim, `mail.views` that a POST
and not a link-scanner's GET is asking. A token is a name, not a permission.

AND A TOKEN MAY ONLY EVER SUBTRACT
----------------------------------
There is no re-subscribe reachable from either of these. A forwarded link must
not let a stranger undo a member's own decision, so turning email back on is
only ever done from inside the app.

WHY NOT django.core.signing
---------------------------
`Signer` is the obvious answer and it emits base64, which is case-sensitive.
Email local parts are case-insensitive in practice - a good number of providers
and forwarders normalise them - and a single folded character turns a valid
signature into a forged one. So the tag is hex, via Django's own `salted_hmac`:
same primitive, an alphabet that survives the trip. Hex costs nothing in a URL
either, and it avoids base64's `+` and `/` needing percent-encoding inside a
`List-Unsubscribe: <...>` header.

NEITHER TOKEN EXPIRES, AND THAT IS DELIBERATE
---------------------------------------------
The opposite of `PASSWORD_RESET_TIMEOUT`, on purpose. An unsubscribe link sits
in a mailbox for as long as the mail does, and people unsubscribe from
months-old email; a link that has expired is a link that does not work, which is
the very thing this exists to fix.

The exposure is bounded: a leaked unsubscribe token stops one group, is
reversible from the app, and is strictly WEAKER than the reply address printed
in the same message - which lets its holder post to that group.

Rotating the signing key invalidates every string already delivered. That is
worse for unsubscribe than for replies, where the next email simply carries a
new address, so the unsubscribe tag is signed with `MAIL_TOKEN_SECRET`, which
defaults to `SECRET_KEY` and can be pinned separately when the day comes.
"""
import logging
import re

from django.conf import settings
from django.utils.crypto import constant_time_compare, salted_hmac

from chat.models import ChannelConsent

logger = logging.getLogger(__name__)

# See the module docstring: these must never be equal.
KEY_SALT = 'mail.reply.token'
UNSUBSCRIBE_KEY_SALT = 'mail.unsubscribe.token'

# 20 hex characters is 80 bits. A forger gets no feedback from a wrong guess -
# both callers answer wrong tokens exactly as they answer right ones - and each
# attempt costs them a delivered email, so this is far more headroom than the
# threat needs while leaving the whole address inside the 64-character limit on
# a local part.
TAG_LENGTH = 20

PREFIX = 'reply+'
SEPARATOR = '.'

_TOKEN = re.compile(r'^([0-9]+)\.([0-9a-f]{%d})$' % TAG_LENGTH)


# --- the reply address -----------------------------------------------------


def token_for(participant_id):
    """The opaque half of the reply address for one membership."""
    return f'{participant_id}{SEPARATOR}{_tag(participant_id)}'


def reply_address(participant):
    """Where a reply to this member's copy should be sent, or None.

    None when no reply domain is configured, which is the honest state on a
    laptop and in any deployment that has not set inbound email up yet. The
    templates ask this question before promising a reply works.
    """
    domain = getattr(settings, 'MAIL_REPLY_DOMAIN', '')
    if not domain:
        return None
    return f'{PREFIX}{token_for(participant.pk)}@{domain}'


def participant_id_from(address):
    """The membership id an inbound reply names, or None if it names none.

    Returns None for every kind of wrong - not our domain, no prefix, a
    mangled token, a bad tag - because the caller does the same thing with all
    of them, which is to drop the message without answering. Distinguishing
    them here would only invite somebody to use the difference as an oracle.
    """
    local = _local_part(address)
    if local is None:
        return None
    return _verified_id(local, KEY_SALT, _tag)


# --- the unsubscribe link --------------------------------------------------


def unsubscribe_token_for(participant_id):
    """The opaque part of the unsubscribe URL for one membership."""
    return f'{participant_id}{SEPARATOR}{_unsubscribe_tag(participant_id)}'


def unsubscribe_url(participant):
    """Where this member stops this group's email, or None.

    None when `API_PUBLIC_URL` is unset, which is the honest state on a laptop.
    The templates ask before printing a link, and `mail/checks.py` refuses a
    deploy that has group email switched on and no public URL to build one
    from - because an unsubscribe nobody can reach is the release blocker this
    whole path exists to close.

    Built by hand rather than through `reverse`, so the printed link does not
    change if the route is ever remounted. It is in somebody's mailbox forever.
    """
    root = getattr(settings, 'API_PUBLIC_URL', '')
    if not root:
        return None
    return f'{root}/mail/unsubscribe/{unsubscribe_token_for(participant.pk)}/'


def participant_id_from_unsubscribe(token):
    """The membership id an unsubscribe token names, or None.

    Uniformly None for every kind of wrong, for the reason given above.
    """
    return _verified_id(
        (token or '').strip().lower(), UNSUBSCRIBE_KEY_SALT, _unsubscribe_tag
    )


# --- the shared half -------------------------------------------------------


def _verified_id(token, salt, tagger):
    """`<id>.<tag>` in, the id out, or None if the tag does not verify."""
    match = _TOKEN.match(token or '')
    if match is None:
        return None

    participant_id, tag = match.group(1), match.group(2)
    if not constant_time_compare(tag, tagger(participant_id)):
        logger.warning('An email token signed with %s did not verify', salt)
        return None
    return int(participant_id)


def _local_part(address):
    """`reply+<token>` out of a full address, lower-cased, or None.

    Parsed with `parseaddr` first, because a To header is not a bare address.
    `"Family via Loopd" <reply+312.abc@reply.loopdn.com>` is the ordinary form
    and rpartition on the raw string would keep the trailing `>`, so every
    reply from a client that writes a display name resolved to nothing and was
    dropped in silence.
    """
    from email.utils import parseaddr

    _, parsed = parseaddr(address or '')
    text = (parsed or address or '').strip().lower()
    if '@' not in text:
        return None

    domain = getattr(settings, 'MAIL_REPLY_DOMAIN', '')
    local, _, host = text.rpartition('@')
    if domain and host != domain.lower():
        # Mail addressed somewhere else entirely. A provider that forwards a
        # whole mailbox will send those too.
        return None

    if not local.startswith(PREFIX):
        return None
    return local[len(PREFIX):]


def _tag(participant_id):
    return salted_hmac(
        KEY_SALT, str(participant_id), algorithm='sha256'
    ).hexdigest()[:TAG_LENGTH]


def _unsubscribe_tag(participant_id):
    """Signs `<id>.email`, not the bare id - see the module docstring."""
    return salted_hmac(
        UNSUBSCRIBE_KEY_SALT,
        f'{participant_id}.{ChannelConsent.Channel.EMAIL}',
        getattr(settings, 'MAIL_TOKEN_SECRET', None) or settings.SECRET_KEY,
        algorithm='sha256',
    ).hexdigest()[:TAG_LENGTH]
