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
    ChannelConsent,
    BlockedFromConversation,
    Message,
    Participant,
    PostingNotAllowed,
)
from chat.services import create_message
from sms import commands
from user.models import MessagingSettings

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
        return _opt_out(participant)

    if command == commands.START:
        return _opt_in(participant)

    if command == commands.HELP:
        return InboundResult(action='help', participant=participant, reply=_help_text())

    # The account's own "Send and receive Loop messages by text". Off, nothing
    # they text any Loop is taken - silently, because a reply is a billed text
    # to somebody who asked for none. STOP, START and HELP above still work.
    # Only for a membership that IS the account: a phone-only row is governed by
    # STOP and START alone, the same as its outbound side in sms/outbound.py.
    if (
        participant is not None
        and participant.user_id
        and not MessagingSettings.of(participant.user).sms_enabled
    ):
        return InboundResult(action='ignored_sms_off', participant=participant)

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

    consent = membership.consent_status(
        participant, ChannelConsent.Channel.SMS
    )

    # They replied STOP to this Loop's number. Until START, nothing passes
    # between this Loop and that number in either direction: the delivery ladder
    # refuses outbound, and this refuses inbound - silently, since anything else
    # is a text to somebody who said stop. It is also why YES cannot undo a STOP.
    if consent == ChannelConsent.Status.OPTED_OUT:
        return InboundResult(action='ignored_opted_out', participant=participant)

    # YES and NO are answers only while there is a question open. That
    # qualifier is the whole of the rule, and leaving it out was a bug in two
    # directions at once.
    #
    # A MEMBER WHO ALREADY AGREED IS TALKING, NOT ANSWERING. "Are you coming?"
    # "No" is the most ordinary exchange there is, and reading it as a command
    # swallowed the message AND silently unsubscribed them - the same failure
    # `commands.py` guards against for "stop by the shop on your way home",
    # one layer further in.
    #
    # AND YES IS NOT A CARRIER OPT-IN. Overwriting OPTED_OUT undid a STOP on
    # the strength of a word the carrier does not recognise for that purpose.
    # Only START may clear a STOP, which is exactly what a carrier audit
    # expects and what `_opt_in` does - and it restores the delivery switches
    # too, so consent and the switches cannot drift apart.
    #
    # An opted-in member who wants this group to stop texting them has MUTE,
    # which is per-group and is advertised in the HELP text. Nothing is lost.
    if command == commands.YES and consent in (
        ChannelConsent.Status.PENDING,
        ChannelConsent.Status.DECLINED,
    ):
        return _group_yes(conversation, participant)

    if command == commands.NO and consent == ChannelConsent.Status.PENDING:
        return _group_no(conversation, participant)

    # Anything else from somebody who has not answered the invitation is
    # NOT a message. They were asked a question and said something that is
    # not an answer - posting "Okay" into their family group because they
    # were unsure what to reply is the failure this prevents.
    if consent == ChannelConsent.Status.PENDING:
        return InboundResult(
            action='ignored_pending_consent', participant=participant
        )

    # And a member who declined this group is not posting to it by text
    # either. They can still read and post in the app - NO is about texts.
    if consent == ChannelConsent.Status.DECLINED:
        return InboundResult(
            action='ignored_declined', participant=participant
        )

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
            'Text to post. HELP for commands, STOP to stop texts from this group.'
        ),
    )


def _group_yes(conversation, participant):
    """YES answers THIS group's invitation, and only this group's.

    Distinct from START, which undoes a STOP to this group's number.
    Reached only from a PENDING or DECLINED state - see the guard in
    `handle_inbound`. A member who declined and later changes their mind sends
    YES again, which is what DECLINED is doing in that pair; a member who
    replied STOP has to send START, because undoing a carrier opt-out is the
    carrier's word to undo and not ours.
    """
    membership.set_channel_consent(
        participant, ChannelConsent.Channel.SMS, ChannelConsent.Status.OPTED_IN
    )
    return InboundResult(
        action='group_opted_in',
        participant=participant,
        reply=(
            f'You are in {conversation.name}. Text to post. '
            'HELP for commands, STOP to stop texts from this group.'
        ),
    )


def _group_no(conversation, participant):
    """NO declines THIS group's invitation. Every other group carries on.

    Not STOP, though both are about this group only: NO answers an invitation
    nobody has agreed to yet, STOP opts a number out of a group it was texting,
    and only START undoes that.

    Reached only while the invitation is unanswered - see the guard in
    `handle_inbound`. Once somebody has agreed, a bare "No" is a word in a
    conversation and MUTE is how they ask this group to go quiet.
    """
    membership.set_channel_consent(
        participant, ChannelConsent.Channel.SMS, ChannelConsent.Status.DECLINED
    )
    return InboundResult(
        action='group_declined',
        participant=participant,
        reply=(
            f'You will not get {conversation.name} by text. '
            'Reply YES to turn it back on. Other groups are unaffected.'
        ),
    )


def _opt_out(participant):
    """Honour STOP from anybody, member or not - for THIS group only.

    STOP to a group's number opts that number out of that group: its SMS
    consent becomes OPTED_OUT and its membership's texts are switched off, so
    the group no longer texts the number and no longer takes texts from it (the
    OPTED_OUT guard in `handle_inbound`). Every other group the number is in,
    and the account's own "Send and receive Loop messages by text", are left
    exactly as they were. Yul's call, 2026-09-15 - to confirm with the telecom
    specialist before release (product plan §17).

    Deliberately does not remove them from the group: STOP is about texts, and
    a member who also uses the web app has not asked to leave anything.
    """
    if participant is not None:
        participant.sms_enabled = False
        participant.save(update_fields=['sms_enabled'])
        membership.set_channel_consent(
            participant,
            ChannelConsent.Channel.SMS,
            ChannelConsent.Status.OPTED_OUT,
        )

        # The admins would otherwise never learn this. From their side the
        # member simply goes quiet, and every message they send from now on
        # silently misses one person.
        from chat.membership import admin_users
        from notification.models import Notification
        from notification.services import notify_many

        notify_many(
            admin_users(participant.conversation),
            Notification.Type.SMS_OPTED_OUT,
            from_user=participant.user,
            conversation=participant.conversation,
            data={'display_name': participant.display_name},
        )

    # No reply: the carrier and the messaging profile send the mandated
    # confirmation themselves, and a second one is a double-ack and a charge.
    return InboundResult(action='opted_out', participant=participant)


def _opt_in(participant):
    """Honour START, the carrier's word for undoing a STOP - for THIS group.

    Undoes a STOP to this group's number and nothing else, as STOP only ever
    touched this group. If the account has "Send and receive Loop messages by
    text" off, the group is back on but still sends nothing, and the reply says
    so rather than promising texts that will not come.
    """
    if participant is None:
        return InboundResult(action='ignored_unknown_sender')

    participant.sms_enabled = True
    participant.save(update_fields=['sms_enabled'])
    # No consent_at write here: set_channel_consent below stamps `consented_at`
    # on the SMS consent row, which is the per-channel record.
    membership.set_channel_consent(
        participant, ChannelConsent.Channel.SMS, ChannelConsent.Status.OPTED_IN
    )

    group = participant.conversation.name
    if participant.user_id and not MessagingSettings.of(participant.user).sms_enabled:
        reply = f"{group} is back on, but texts are off in your Loopd'n settings."
    else:
        reply = f'You will get {group} by text again. Text STOP to opt out.'
    return InboundResult(action='opted_in', participant=participant, reply=reply)


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
