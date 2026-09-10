"""What happens when somebody replies to one of our emails.

One reply in, one decision out. No HTTP: the webhook hands this an
already-verified payload, and everything below is database work a test can
drive directly - the same split `sms/inbound.py` uses, for the same reason.

WHAT THE ADDRESS PROVES, AND WHAT IT DOES NOT
---------------------------------------------
The reply address names a membership. That is all it does. It is not a
credential and is not treated as one, because an email address travels: it is
in the headers of every copy of that message, in every forward of it, and in
the sent folder of anybody the recipient showed it to.

So the token says WHO to check and the checks below decide whether to accept:

    the token resolves to a membership   -> otherwise silence
    that membership is still ACTIVE      -> a member who left cannot post
    the account is not banned
    the FROM address is that member's own verified address
    the group takes messages at all

The fourth is meant to be the one doing the real work. Whoever holds the
address can send mail to it; only the person who owns the mailbox can send mail
FROM it that survives SPF and DKIM - which are checked upstream, by the inbound
provider, and are the whole reason the from address is worth anything.

    NOT YET ENFORCED. `mail/webhooks.py:_extract` discards the headers, so
    nothing here reads the provider's verdict and the guarantee above rests on
    the provider refusing to forward unauthenticated mail at all. Until the
    verdict is read and acted on, inbound email must stay switched off:
    otherwise anybody who learns a reply address can post to that group as that
    member. See the known gaps in docs/multi_channel_integration_plan.md.

MUTE AND ARCHIVE DO NOT APPLY HERE
----------------------------------
Both are about being notified. Neither is about taking part, and a muted member
replying to an old email is still a member with something to say - see §3 of
the use cases, which states this explicitly for exactly this path.

SILENCE IS THE ANSWER TO EVERYTHING WE REFUSE
---------------------------------------------
Nothing here bounces or replies. A refusal that answers tells whoever is
probing that the address is live and the group is real, and an automated reply
to an automated sender is a loop that neither end stops.
"""
import logging

from django.db import transaction

from chat import membership
from chat.models import Message, Participant, PostingNotAllowed
from chat.services import create_message
from mail.quoting import strip_quoted_reply
from mail.tokens import participant_id_from

logger = logging.getLogger(__name__)


class InboundResult:
    """What was done with one inbound email."""

    def __init__(self, action, participant=None, message=None):
        self.action = action
        self.participant = participant
        self.message = message

    def __repr__(self):
        return f'<InboundResult {self.action}>'


def resolve_recipient(to_address):
    """The membership an inbound reply belongs to, or None.

    `to_address` is the address the reply was sent TO - ours - not the sender's.
    """
    participant_id = participant_id_from(to_address)
    if participant_id is None:
        return None

    return (
        Participant.objects.filter(pk=participant_id)
        .select_related('conversation', 'user__profile')
        .first()
    )


@transaction.atomic
def handle_inbound(to_address, from_address, body):
    """Act on one inbound email. Returns an InboundResult."""
    participant = resolve_recipient(to_address)

    if participant is None:
        return InboundResult(action='ignored_unknown_recipient')

    if participant.status != Participant.Status.ACTIVE:
        # They left, were removed, or were blocked. §3 is explicit that a reply
        # from any of them is rejected - the address outlives the membership.
        return InboundResult(action='ignored_not_a_member', participant=participant)

    # Checked at the action rather than in the lookup, exactly as the SMS path
    # does it: a banned account must still be FOUND, or it stops being a member
    # at all and the next thing that resolves it hands back a fresh row.
    if not membership.account_is_usable(participant):
        return InboundResult(action='ignored_banned_account', participant=participant)

    if not _sender_is_the_member(participant, from_address):
        logger.warning(
            'Inbound email for participant %s came from another address',
            participant.pk,
        )
        return InboundResult(action='ignored_address_mismatch', participant=participant)

    text = strip_quoted_reply(body)
    if not text:
        # An auto-reply, a read receipt, or a message that was nothing but the
        # thread quoted back. `create_message` has no opinion but the
        # serializer forbids empty messages, so this path has to agree.
        return InboundResult(action='ignored_empty', participant=participant)

    try:
        message = create_message(
            conversation=participant.conversation,
            sender=participant.user,
            text=text,
            source=Message.Source.EMAIL,
            participant=participant,
        )
    except PostingNotAllowed:
        # A broadcast group. Silence rather than a bounce, per the module
        # docstring; they can see why in the app.
        return InboundResult(action='refused_broadcast', participant=participant)

    return InboundResult(action='posted', participant=participant, message=message)


def _sender_is_the_member(participant, from_address):
    """Whether this reply really came from the member the address names.

    Requires the account's own VERIFIED address, and nothing else. An
    unverified one is a string somebody typed into their profile, so accepting
    it would let anybody who can type your address post to your groups.

    Aliases and plus-addressing are not accepted. `alice+groups@x.com` reaches
    the same mailbox as `alice@x.com` and a mail client can be configured to
    send as either, so a member using one will find their reply silently
    dropped - that is a real limitation, and the alternative is a rule about
    which parts of an address may differ, which is a rule about who may post.
    """
    address = _address_only(from_address)
    if not address:
        return False

    if participant.user_id is None:
        return False

    profile = getattr(participant.user, 'profile', None)
    if profile is None or not profile.email_verified:
        return False

    return address == (participant.user.email or '').strip().lower()


def _address_only(value):
    """`alice@x.com` out of `Alice <Alice@X.com>`, lower-cased."""
    from email.utils import parseaddr

    _, address = parseaddr(value or '')
    return address.strip().lower()
