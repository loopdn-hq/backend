"""What happens when a text arrives for a group.

One text in, one decision out. No HTTP: the provider webhook will hand this
function an already-verified payload, and everything below here is database work
that a test can drive directly.

ORDER IS THE DESIGN
-------------------
The sequence in `handle_inbound` is not arbitrary and should not be rearranged
for tidiness:

  1. STOP and its carrier synonyms, first and unconditionally. A carrier audit
     tests these literally, from numbers that are not members, at times when the
     group does not exist. They must never depend on anything above them.
  2. START, for the same reason.
  3. HELP, likewise mandated.
  4. JOIN - the front door. It is the one command a non-member may use.
  5. Everything else requires an active membership, because everything else
     either reads the group or writes to it.

REPLIES COST MONEY, AND CONFIRM THE NUMBER IS LIVE
--------------------------------------------------
Every reply is a billed message to an arbitrary number that texted us, so this
module returns reply text rather than sending it, and returns None far more
often than not. An unknown sender is answered with silence: a reply would tell
whoever is probing that the number is real and that the group exists.

STOP is answered with silence too - not rudeness, but because the carrier and
the messaging profile already send the mandated confirmation, and a second one
is both a double-ack and a second charge.
"""
import hashlib
import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from chat import membership
from chat.models import (
    BlockedFromConversation,
    Message,
    Participant,
    PostingNotAllowed,
)
from chat.services import create_message
from sms import commands

logger = logging.getLogger(__name__)


@dataclass
class InboundResult:
    """What was done, and what (if anything) to text back."""

    action: str
    participant: object = None
    message: object = None
    reply: str = None


def resolve_sender(conversation, phone_number):
    """The membership this number belongs to in this conversation, or None.

    Delegates to `membership.resolve`, which is the ONE place that decides which
    row represents a person - BLOCKED first, then ACTIVE, then PENDING. This
    function used to pick the first ACTIVE row instead, which is a second and
    contradictory answer to the same question: somebody holding both a blocked
    account row and an active phone row was blocked according to the REST path
    and free to post according to this one.
    """
    return membership.resolve(conversation, phone_number=phone_number)


@transaction.atomic
def handle_inbound(conversation, phone_number, body, *, display_name=''):
    """Act on one inbound text. Returns an InboundResult."""
    command, argument = commands.parse(body)
    participant = resolve_sender(conversation, phone_number)

    if command == commands.STOP:
        return _opt_out(participant, phone_number)

    if command == commands.START:
        return _opt_in(participant)

    if command == commands.HELP:
        return InboundResult(action='help', participant=participant, reply=_help_text())

    already_in = participant is not None and participant.status in (
        Participant.Status.ACTIVE,
        Participant.Status.PENDING,
    )

    # JOIN is the front door, and only a command for somebody still outside it.
    # To a member who is already in, "Join us at 6pm tomorrow" is a message to
    # their family - and treating it as a command would swallow the message,
    # send them a baffling confirmation, and charge us for the privilege.
    if command == commands.JOIN and not already_in:
        return _join(conversation, phone_number, argument, display_name)

    # A pending joiner texting JOIN again is asking where their request got to.
    # Silence is reserved for strangers; they are not one.
    if command == commands.JOIN and participant.status == Participant.Status.PENDING:
        return InboundResult(
            action='join_pending',
            participant=participant,
            reply=f'Your request to join {conversation.name} is still with the admins.',
        )

    # Past here, only members. Silence for everyone else - a reply would
    # confirm the number is live and the group exists.
    if participant is None or participant.status != Participant.Status.ACTIVE:
        return InboundResult(action='ignored_unknown_sender')

    # The ban is checked here, at the action, and deliberately NOT inside the
    # identity lookup: a banned person must still be FOUND, or they stop being
    # a member at all and the next JOIN hands them a brand new row.
    if not membership.account_is_usable(participant):
        return InboundResult(action='ignored_banned_account', participant=participant)

    if command == commands.MUTE:
        membership.set_muted(participant, True)
        return InboundResult(
            action='muted',
            participant=participant,
            reply=f'Muted {conversation.name}. Text UNMUTE to turn it back on.',
        )

    if command == commands.UNMUTE:
        membership.set_muted(participant, False)
        return InboundResult(
            action='unmuted',
            participant=participant,
            reply=f'Unmuted {conversation.name}.',
        )

    if command == commands.LEAVE:
        try:
            membership.leave(participant)
        except (membership.JoinRefused, membership.NotAMember) as exc:
            # The sole admin of a group cannot leave it - they have to hand over
            # first. Letting that escape would 500 the webhook, and a carrier
            # reads a 500 as "retry", so the same text would arrive again and
            # again.
            return InboundResult(
                action='refused', participant=participant, reply=str(exc)
            )
        return InboundResult(
            action='left',
            participant=participant,
            reply=f'You have left {conversation.name}.',
        )

    return _post(conversation, participant, body)


# --- the individual outcomes ----------------------------------------------


def _post(conversation, participant, body):
    if not conversation.sms_replies_enabled:
        # The group receives texts but does not accept them - announcements
        # only. Worth one reply: they are a member in good standing who has
        # just been ignored, and silence reads as the service being broken.
        return InboundResult(
            action='refused_replies_off',
            participant=participant,
            reply=f'{conversation.name} does not take replies by text.',
        )

    text = (body or '').strip()
    if not text:
        # An empty body is an MMS with no caption, or a message that was
        # nothing but whitespace. `create_message` has no opinion but the
        # serializer forbids empty messages, so the SMS path must agree.
        return InboundResult(action='ignored_empty', participant=participant)

    try:
        message = create_message(
            conversation=conversation,
            sender=participant.user,
            text=text,
            source=Message.Source.SMS,
            participant=participant,
        )
    except PostingNotAllowed:
        # A broadcast group. Worth one reply: they are a member in good
        # standing who has just been ignored, and silence would read as the
        # service being broken.
        return InboundResult(
            action='refused_broadcast',
            participant=participant,
            reply=f'{conversation.name} is announcements only.',
        )
    # No reply. The sender's own phone already shows what they sent, and the
    # fan-out deliberately excludes them.
    return InboundResult(action='posted', participant=participant, message=message)


def _join(conversation, phone_number, code, display_name):
    if not _join_attempt_allowed(phone_number):
        # Silence, not an explanation. A number guessing codes learns nothing
        # from being told it is guessing too fast.
        logger.warning('Join attempt cap reached for ...%s', phone_number[-4:])
        return InboundResult(action='refused_rate_limited')

    try:
        participant = membership.request_to_join(
            conversation,
            phone_number=phone_number,
            # Passed through empty when nobody gave a name, so that
            # request_to_join stores a MASKED number rather than the raw one.
            # Defaulting to phone_number here would publish it to the group.
            display_name=display_name,
            code=code,
        )
    except BlockedFromConversation:
        # Silence, not an explanation. Telling a blocked number that it is
        # blocked invites them to try from another one.
        return InboundResult(action='refused_blocked')
    except (membership.JoinRefused, membership.NotAMember) as exc:
        return InboundResult(action='refused', reply=str(exc))

    if participant.status == Participant.Status.PENDING:
        return InboundResult(
            action='join_pending',
            participant=participant,
            reply=f'Your request to join {conversation.name} is with the admins.',
        )

    return InboundResult(
        action='joined',
        participant=participant,
        reply=(
            f'You are in {conversation.name}. '
            'Text to post. HELP for commands, STOP to leave.'
        ),
    )


def _opt_out(participant, phone_number):
    """Honour STOP from anybody, member or not.

    Deliberately does not remove them from the group: STOP is about texts, and
    a member who also uses the web app has not asked to leave anything. It does
    stop every message we would otherwise send to this number.
    """
    if participant is not None:
        participant.sms_enabled = False
        participant.sms_consent = Participant.Consent.STOPPED
        participant.save(update_fields=['sms_enabled', 'sms_consent'])

        profile = getattr(participant.user, 'profile', None) if participant.user_id else None
        if profile is not None:
            profile.sms_enabled = False
            profile.sms_opt_out_at = timezone.now()
            profile.save(update_fields=['sms_enabled', 'sms_opt_out_at', 'updated_at'])

    # No reply: the carrier and the messaging profile send the mandated
    # confirmation themselves, and a second one is a double-ack and a charge.
    return InboundResult(action='opted_out', participant=participant)


def _opt_in(participant):
    if participant is None:
        return InboundResult(action='ignored_unknown_sender')

    participant.sms_enabled = True
    participant.sms_consent = Participant.Consent.GRANTED
    participant.consent_at = timezone.now()
    participant.save(
        update_fields=['sms_enabled', 'sms_consent', 'consent_at']
    )

    profile = getattr(participant.user, 'profile', None) if participant.user_id else None
    if profile is not None:
        profile.sms_enabled = True
        profile.sms_consent_at = timezone.now()
        profile.save(update_fields=['sms_enabled', 'sms_consent_at', 'updated_at'])

    return InboundResult(
        action='opted_in',
        participant=participant,
        reply='You are subscribed again. Text STOP to opt out.',
    )


def _help_text():
    # One segment of GSM-7. Every character here is billed to us on every HELP,
    # and HELP is the one message an abusive sender can trigger repeatedly.
    return 'LoopD: text to post. JOIN, MUTE, UNMUTE, LEAVE, STOP.'


def _join_attempt_allowed(phone_number):
    """Whether this number may try to join again this hour.

    A join code is six characters from a 29-letter alphabet, which is plenty
    against a person and nothing at all against a number that can guess without
    limit. Counted per sender, hashed because cache keys surface in logs.
    """
    target = hashlib.sha256((phone_number or '').encode()).hexdigest()[:32]
    key = f'sms:join:h:{target}'
    if (cache.get(key) or 0) >= settings.SMS_MAX_JOIN_ATTEMPTS_PER_HOUR:
        return False
    cache.add(key, 0, 3600)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, 3600)
    return True
