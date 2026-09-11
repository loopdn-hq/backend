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
    the receiving server vouches for the FROM address
    the FROM address is that member's own verified address
    the group takes messages at all

The fourth and fifth do the real work, and only together. Whoever holds the
address can send mail TO it; only the person who owns the mailbox can send mail
FROM it that the receiving server marks as authenticated. That verdict - SPF,
DKIM, DMARC - is read off the message headers by `mail/authentication.py` and
asked here immediately before the From address is compared, because a From
address nobody has vouched for is a string anybody can type.
`MAIL_INBOUND_REQUIRE_AUTHENTICATION` switches the question off for a laptop
feeding the webhook by hand, and mail/checks.py refuses to deploy that.

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

from django.conf import settings
from django.db import transaction

from chat import membership
from chat.models import Message, Participant, PostingNotAllowed
from chat.services import create_message
from mail import authentication, autoreply, commands
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
def handle_inbound(to_address, from_address, body, *, headers, subject=''):
    """Act on one inbound email. Returns an InboundResult.

    `headers` is the message's headers, keyed lower-case, and is required
    rather than defaulted for the reason `sms/outbound.py:still_eligible` gives:
    a default would let a caller skip the authentication check by forgetting,
    and "did we check?" must never be answerable by reading a call site.
    `subject` only helps `mail/autoreply.py` catch a responder that set no
    header, so it may be left out.
    """
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

    if not _from_is_authenticated(headers, from_address):
        logger.warning(
            'Inbound email for participant %s failed sender authentication',
            participant.pk,
        )
        return InboundResult(action='ignored_unauthenticated', participant=participant)

    if not _sender_is_the_member(participant, from_address):
        logger.warning(
            'Inbound email for participant %s came from another address',
            participant.pk,
        )
        return InboundResult(action='ignored_address_mismatch', participant=participant)

    if autoreply.is_automatic(headers, subject):
        # A vacation responder answering from the member's own address passes
        # every check above. Posting it would fan "I am away until Monday" to
        # the whole group and invite the next responder to answer that.
        return InboundResult(action='ignored_auto_reply', participant=participant)

    text = strip_quoted_reply(body)
    if not text:
        # A read receipt, or a message that was nothing but the thread quoted
        # back. `create_message` has no opinion but the serializer forbids
        # empty messages, so this path has to agree.
        return InboundResult(action='ignored_empty', participant=participant)

    # AFTER the quote-stripping, and the order is not incidental. Every group
    # email prints the keyword in its footer, so most replies carry it in the
    # thread underneath; asking first would read our own instructions back as a
    # command. The footer is introduced by `-- `, which `mail/quoting.py` cuts
    # at, so by here it is gone.
    #
    # And after the identity checks above, unlike SMS's STOP - which
    # `sms/inbound.py` answers first and unconditionally because a carrier
    # audits it from numbers that are not members. There is no such mandate
    # here, nothing to do for a sender we cannot identify, and answering one
    # would confirm the address is live.
    if commands.is_stop_emails(text):
        return _stop_emails(participant)

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


def _from_is_authenticated(headers, from_address):
    """Whether the receiving server vouched for the From address.

    Asked BEFORE the address is compared with the member's, not after: the
    comparison only means something for an address somebody has authenticated,
    and the other order would log an "address mismatch" for what is really a
    forgery. The rule itself is mail/authentication.py's; this only decides
    whether to ask it.
    """
    if not settings.MAIL_INBOUND_REQUIRE_AUTHENTICATION:
        return True
    return authentication.from_is_trusted(headers, from_address)


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


def _stop_emails(participant):
    """A member replied STOP EMAILS. Stop this group, and say so.

    One group, never the account: they may be in eight and only tired of one,
    and a product that makes somebody leave all eight to quieten one loses a
    channel it was allowed to use. That distinction is the whole reason the
    keyword is two words and is not the word "unsubscribe".

    The confirmation is scheduled for after the commit, so nothing is sent
    about a preference that then rolls back - and it is the one path that gets
    an email back, because this is the one where the person cannot see a page.
    See mail/optout.py.
    """
    changed = membership.decline_email(participant)

    if changed:
        from mail.optout import send_confirmation

        transaction.on_commit(lambda: send_confirmation(participant))

    return InboundResult(action='email_declined', participant=participant)
