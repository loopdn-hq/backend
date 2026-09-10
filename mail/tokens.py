"""The address a group's email invites a reply to, and reading one back.

    reply+312.4f1c9a0b83d7e6512aa4@reply.loopdn.com

The token identifies ONE membership - one person, in one group. That is what
the use cases ask for and it is also the only shape that is safe: an address
built from the conversation alone (`group-123@...`) is a public write handle to
a private group, and anybody who is forwarded one message can post to the group
forever.

SIGNED, NOT STORED
------------------
There is no token table. The token is the membership id with an HMAC of it
alongside, so the address is derived on the way out and verified on the way
back with no row to create, expire or clean up. Nothing is trusted from the
address itself: the id only says which membership to look at, and
`mail.inbound` then re-checks that the membership is live, that the sender is
who the address claims, and that the group takes replies. A token is a name,
not a permission.

WHY NOT django.core.signing
---------------------------
`Signer` is the obvious answer and it emits base64, which is case-sensitive.
Email local parts are case-insensitive in practice - a good number of providers
and forwarders normalise them - and a single folded character turns a valid
signature into a forged one. So the tag is hex, via Django's own
`salted_hmac`: same primitive, same secret, an alphabet that survives the trip.

ROTATING SECRET_KEY invalidates every address already in somebody's mailbox.
Replies to old mail stop resolving and are answered with silence; the next
message carries a new address. That is the accepted cost of not keeping a
table, and it is worth knowing before a key rotation rather than after.
"""
import logging
import re

from django.conf import settings
from django.utils.crypto import constant_time_compare, salted_hmac

logger = logging.getLogger(__name__)

# Part of the signed material, so a token minted here can never be replayed at
# some other salted_hmac call site that happens to sign the same number.
KEY_SALT = 'mail.reply.token'

# 20 hex characters is 80 bits. A forger gets no feedback from a wrong guess -
# the reply is silently dropped - and every attempt costs them a delivered
# email, so this is far more headroom than the threat needs while leaving the
# whole address inside the 64-character limit on a local part.
TAG_LENGTH = 20

PREFIX = 'reply+'
SEPARATOR = '.'

_TOKEN = re.compile(r'^([0-9]+)\.([0-9a-f]{%d})$' % TAG_LENGTH)


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

    match = _TOKEN.match(local)
    if match is None:
        return None

    participant_id, tag = match.group(1), match.group(2)
    if not constant_time_compare(tag, _tag(participant_id)):
        logger.warning('Inbound email carried a reply token that does not verify')
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
