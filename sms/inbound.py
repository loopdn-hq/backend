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
from user.models import Profile

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
        return _opt_in(participant, phone_number)

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

    consent = membership.consent_status(
        participant, ChannelConsent.Channel.SMS
    )

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
            'Text to post. HELP for commands, STOP to leave.'
        ),
    )


def _group_yes(conversation, participant):
    """YES answers THIS group's invitation, and only this group's.

    Distinct from START, which is the carrier's word for all of LoopD'n.
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
            'HELP for commands, NO to stop this group, STOP to leave LoopD.'
        ),
    )


def _group_no(conversation, participant):
    """NO declines THIS group's invitation. Every other group carries on.

    Not STOP: that is the carrier-level opt-out and covers the whole product.
    Somebody who wants out of one noisy group should not have to leave all of
    them, and conflating the two is how a product loses a channel it was
    allowed to use.

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


def _opt_out(participant, phone_number):
    """Honour STOP from anybody, member or not.

    Deliberately does not remove them from the group: STOP is about texts, and
    a member who also uses the web app has not asked to leave anything. It does
    stop every message we would otherwise send to this number.
    """
    if participant is not None:
        participant.sms_enabled = False
        participant.save(update_fields=['sms_enabled'])
        membership.set_channel_consent(
            participant,
            ChannelConsent.Channel.SMS,
            ChannelConsent.Status.OPTED_OUT,
        )
        # The number, not the row. One call now covers the membership rows,
        # the account's profile switch, and every other group either can reach.
        _carry_to_every_group(participant, phone_number, enabled=False)

        # The admins would otherwise never learn this. From their side the
        # member simply goes quiet, and every message they send from now on
        # silently misses one person.
        from chat.membership import admin_users
        from chat.models import Notification
        from chat.notifications import notify_many

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


def _opt_in(participant, phone_number):
    """Honour START, the carrier's word for undoing a STOP.

    Takes the inbound number rather than reading it off the row: an account
    row's own `phone_number` is None - the number lives on the profile - so
    keying on the row meant a START sent from an account restored nothing.
    """
    if participant is None:
        return InboundResult(action='ignored_unknown_sender')

    participant.sms_enabled = True
    participant.consent_at = timezone.now()
    participant.save(update_fields=['sms_enabled', 'consent_at'])
    membership.set_channel_consent(
        participant, ChannelConsent.Channel.SMS, ChannelConsent.Status.OPTED_IN
    )
    _carry_to_every_group(participant, phone_number, enabled=True)

    return InboundResult(
        action='opted_in',
        participant=participant,
        reply='You are subscribed again. Text STOP to opt out.',
    )


def _carry_to_every_group(participant, phone_number, *, enabled):
    """Carry a STOP or a START to everything this NUMBER can reach.

    STOP is the carrier's word. It covers all of LoopD'n, not the group whose
    number happened to receive it, and an audit tests exactly that.

    KEYED ON THE NUMBER, NOT ON THE KIND OF ROW THAT ANSWERED. One human can
    hold an account row in Family and a phone-only row in Work under the same
    verified number - `chat/membership.py:memberships_for` exists because of
    precisely that. The previous version asked which kind of row had received
    the text and swept only the matching kind, so either arrangement left the
    other half still texting a number that had just said STOP:

        account row answers   -> phone-only rows elsewhere were never swept
        phone-only answers    -> the account's profile switch was never written

    Both are the same violation from opposite ends, and each group has its own
    sending number, so the carrier's per-number opt-out does not cover the
    others either.

    IT MOVES SWITCHES, NOT ANSWERS. `sms_enabled` on the membership and
    `Profile.sms_enabled` on the account, never the ChannelConsent rows.
    Overwriting every group's ANSWER would lose the difference between
    "declined Family" and "never asked about Work", and the matching START
    would hand all of them back as opted in. A switch is the right shape for a
    fact about a number; an answer belongs to a person and a group.

    KNOWN LIMIT: because one switch carries both meanings, a START restores
    `sms_enabled` on rows an admin had deliberately switched off. That is the
    narrow cost of not having a separate number-level record, and it is the
    right way round - a STOP that under-silences is a compliance failure, a
    START that over-restores is an annoyance.

    Returns how many membership rows it moved.
    """
    if not phone_number:
        return 0

    moved = Participant.objects.filter(
        phone_number=phone_number, user__isnull=True
    ).update(sms_enabled=enabled)

    stamp = timezone.now()

    # The account that verified this number, if there is one. Its profile
    # switch is what `sms/outbound.py:_address_reason` reads for every account
    # row, so writing it covers every group they are in at once - including the
    # ones this text did not come from.
    profiles = list(
        Profile.objects.filter(
            phone_number=phone_number, phone_no_verified=True
        )
    )

    # And the answering account itself. Normally the query above already found
    # it, but a row can only be resolved from a verified number in the first
    # place - this is belt and braces on the one path a carrier audits.
    own = getattr(participant.user, 'profile', None) if participant.user_id else None
    if own is not None and not any(p.pk == own.pk for p in profiles):
        profiles.append(own)

    for profile in profiles:
        profile.sms_enabled = enabled
        if enabled:
            profile.sms_consent_at = stamp
            fields = ['sms_enabled', 'sms_consent_at', 'updated_at']
        else:
            profile.sms_opt_out_at = stamp
            fields = ['sms_enabled', 'sms_opt_out_at', 'updated_at']
        profile.save(update_fields=fields)

    return moved


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
