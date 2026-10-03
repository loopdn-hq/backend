"""Sending a group's messages out over SMS.

One message in, many texts out - the fan-out. A post to a ten-person group is
nine outbound messages, each one a real charge, so almost everything in this
module is about deciding NOT to send.

WHERE THE PROVIDER LIVES
------------------------
Behind `sms.client.send_text`. Per the product plan (§15, §20) the provider,
number strategy and carrier rules belong to the telecom specialist, and core
code talks to them through a normalised call. With nothing configured that call
logs instead of sending and reports a synthetic id, so this whole path runs end
to end - and is tested - with no account, no credentials and no network.

WHAT THIS WILL NOT DO
---------------------
Send to the person who wrote the message. Send to somebody who muted the group,
opted out, has no verified number, or is not an active member. Send once a
budget is spent. Each of those is a row in the delivery log with a reason, not a
silent omission, because "why didn't I get that" is a support question somebody
will ask.
"""
import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from chat import delivery, membership
from chat.models import ChannelConsent, Participant
from sms import segments
from sms.client import SmsError, is_allowed_destination, send_text
from sms.models import GroupNumber, SmsDelivery, SmsSettings
from user.models import MessagingSettings

logger = logging.getLogger(__name__)


def format_message(conversation, message):
    """The text a recipient actually sees.

    `Family - Yul: Dinner at 7`. The separator is a hyphen and not an em dash,
    which is not a style preference: U+2014 is in neither the GSM-7 basic
    alphabet nor its extension table, so one would drop EVERY message this
    product sends from 160-character segments to 70-character ones. A test
    asserts the fixed parts stay GSM-7 so it cannot come back by copy-paste.
    """
    who = message.sender_label
    body = (message.text or '').strip()
    if not body:
        # A photo posted from the app has no text. Without this the recipient
        # gets "Family - alice:" and no idea anything was shared - worse than
        # useless, because it looks like a bug in the product.
        body = _attachment_summary(message)
    return f'{conversation.name} - {who}: {body}'


def _attachment_summary(message):
    count = message.attachments.count()
    if count == 0:
        return '(no message)'
    if count == 1:
        return '(sent a photo or file - open LoopD to see it)'
    return f'(sent {count} photos or files - open LoopD to see them)'


def recipients_for(message):
    """Who should receive this message as a text, and who should not.

    Returns (send_to, skipped) where `skipped` carries a reason per person, so
    the delivery log can answer for its own omissions.
    """
    send_to, skipped = delivery.eligible_for(
        message, ChannelConsent.Channel.SMS
    )

    # The shared ladder answers the membership question. Whether there is a
    # number we may text is SMS's own, and is asked here.
    textable, no_address = [], []
    for row in send_to:
        reason = _address_reason(row)
        if reason is None:
            textable.append(row)
        else:
            no_address.append((row, reason))

    return textable, skipped + no_address


def _address_reason(participant):
    """Why there is no number we may text, or None if there is one.

    Only the address question - membership, muting and consent were settled by
    chat.delivery before this is reached.
    """
    if participant.user_id is None:
        number = participant.phone_number
    else:
        # An account member is texted on the number they verified when they
        # signed up. Verification is the whole point of collecting it: an
        # unverified number is a string somebody typed, and texting it would
        # send this group's messages to whoever really owns it.
        profile = getattr(participant.user, 'profile', None)
        # "Send and receive Loop messages by text", off in the app. A STOP is
        # not this: it is per group, and the delivery ladder already refused it.
        if profile is None or not MessagingSettings.of(participant.user).sms_enabled:
            return 'sms disabled account-wide'
        if not profile.phone_no_verified or not profile.phone_number:
            return 'no verified number'
        number = profile.phone_number

    if not number:
        return 'no number'
    if not is_allowed_destination(number):
        # Defence in depth. A verified number cleared this once, but the
        # allowlist narrows, and premium ranges hide inside ordinary prefixes.
        return 'destination not allowed'
    return None


def still_eligible(participant, message):
    """Why this member must not be texted RIGHT NOW, or None if they may be.

    All three parts of the decision - the shared membership ladder, whether the
    sender is somebody they have blocked, and SMS's own address question -
    which together are exactly what `recipients_for` asks for a fresh fan-out.

    It exists as its own function because the fan-out is not the only thing
    that sends: `retry_delivery` picks a row up minutes or hours later, and the
    world moves in between. A decision taken at fan-out time is not still true
    when the retry runs - somebody can block the sender inside that window.

    `message` is REQUIRED rather than defaulted, and that is the point. A
    default would let a caller omit it and get a check that silently skipped
    the block, which is worse than no check at all because it looks like one.
    Asked again at every send, because "did we check?" should never be
    answerable by reading how a function was called.

    The composition itself lives in `chat/delivery.py:retry_refusal`, shared
    with email, so the two channels cannot come to re-decide a retry
    differently. Only the address question here is SMS's own.
    """
    return delivery.retry_refusal(
        participant, message, ChannelConsent.Channel.SMS, _address_reason
    )


def number_for(participant):
    """The E.164 to text for this member."""
    if participant.user_id is None:
        return participant.phone_number
    profile = getattr(participant.user, 'profile', None)
    return profile.phone_number if profile else None


def fan_out(message):
    """Send one message to everybody who should get it as a text.

    Returns the SmsDelivery rows it created. Safe to call when SMS is not
    configured at all: the provider call logs instead of sending, so the whole
    decision path still runs and is still logged.
    """
    conversation = message.conversation

    # Idempotent PER RECIPIENT, not per message. A retried dispatch must not
    # text anybody twice - the provider call is already charged for by the time
    # the unique constraint would fire - but it must still FINISH the job. A
    # fan-out that died after three of ten people would otherwise count as
    # "already done" forever, and seven members would simply never get the
    # message with nothing anywhere saying so.
    already = set(
        SmsDelivery.objects.filter(
            message=message, direction=SmsDelivery.Direction.OUTBOUND
        ).values_list('participant_id', flat=True)
    )

    if not SmsSettings.sending_is_on():
        # The runtime kill switch. Recorded rather than silently dropped, so
        # that "nobody got my message" has an answer in the log.
        send_to, _ = recipients_for(message)
        return [
            _record(message, row, SmsDelivery.Status.SKIPPED,
                    reason='sending is switched off')
            for row in send_to
            if row.pk not in already
        ]

    if not conversation.sms_delivery_enabled:
        # The group's own outbound switch, above every member's. Recorded per
        # person for the same reason: a silent non-delivery is a support ticket.
        send_to, _ = recipients_for(message)
        return [
            _record(message, row, SmsDelivery.Status.SKIPPED,
                    reason='sms is off for this group')
            for row in send_to
            if row.pk not in already
        ]

    group_number = GroupNumber.for_number_of(conversation)
    send_to, skipped = recipients_for(message)

    if already:
        total = len(send_to) + len(skipped)
        send_to = [row for row in send_to if row.pk not in already]
        skipped = [pair for pair in skipped if pair[0].pk not in already]
        remaining = len(send_to) + len(skipped)
        if not remaining:
            logger.info('Fan-out for message %s already done', message.pk)
            return []
        logger.warning(
            'Resuming fan-out for message %s: %s of %s recipients still to go',
            message.pk, remaining, total,
        )

    rows = [
        _record(message, row, SmsDelivery.Status.SKIPPED, reason=reason)
        for row, reason in skipped
    ]

    if group_number is None:
        # No number means no way to send and no from-address. Recorded rather
        # than dropped, so a group whose provisioning failed is visible.
        rows += [
            _record(message, row, SmsDelivery.Status.SKIPPED, reason='group has no number')
            for row in send_to
        ]
        return rows

    if len(send_to) > settings.SMS_MAX_RECIPIENTS_PER_MESSAGE:
        # Everybody still gets it in the app; only the SMS leg stops. Recorded
        # per person so the reason is visible rather than the messages simply
        # never arriving.
        logger.error(
            'Conversation %s has %s SMS recipients, over the cap of %s; not sending',
            conversation.pk, len(send_to), settings.SMS_MAX_RECIPIENTS_PER_MESSAGE,
        )
        return rows + [
            _record(message, row, SmsDelivery.Status.SKIPPED,
                    reason='group is too large to text')
            for row in send_to
        ]

    body = format_message(conversation, message)
    count, encoding = segments.count_segments(body)
    if encoding != segments.GSM7:
        # One curly quote in a group name more than doubles the bill for every
        # message that group ever sends. Worth a line in the log.
        logger.warning(
            'SMS for conversation %s is %s (%s segments): %r',
            conversation.pk, encoding, count, body[:40],
        )

    for row in send_to:
        allowed, reason = _within_budget(conversation, count)
        if not allowed:
            rows.append(
                _record(message, row, SmsDelivery.Status.SKIPPED, reason=reason)
            )
            continue
        rows.append(_send_one(message, row, group_number, body, count, encoding))
    return rows


def _send_one(message, participant, group_number, body, count, encoding):
    number = number_for(participant)
    delivery = _record(
        message,
        participant,
        SmsDelivery.Status.SENDING,
        segment_count=count,
        encoding=encoding,
    )

    try:
        result = send_text(to=number, from_number=group_number.number, text=body)
    except SmsError as exc:
        # One bad number must not stop the other nine people getting the
        # message, so this is caught per recipient and never re-raised.
        delivery.status = SmsDelivery.Status.FAILED
        delivery.reason = str(exc)[:120]
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return delivery

    _record_spend(message.conversation, count)
    delivery.status = SmsDelivery.Status.SENT
    delivery.provider = result.get('provider', '')
    delivery.provider_message_id = result.get('id') or ''
    if result.get('segments'):
        # The provider counted it for real; ours was an estimate.
        delivery.segments = result['segments']
    delivery.save(
        update_fields=['status', 'provider', 'provider_message_id', 'segments',
                       'updated_at']
    )
    return delivery


def _record(message, participant, status, *, reason='', segment_count=0, encoding=''):
    number = number_for(participant) or ''
    return SmsDelivery.objects.create(
        direction=SmsDelivery.Direction.OUTBOUND,
        conversation=message.conversation,
        message=message,
        participant=participant,
        user=participant.user,
        counterpart_last4=number[-4:],
        counterpart_hash=_hash_number(number),
        status=status,
        reason=reason,
        segments=segment_count,
        encoding=encoding,
    )


def record_inbound(conversation, participant, message, number, provider='', *,
                   text=None, provider_message_id='', cost=None):
    """Log a text that arrived. Inbound costs money too, and it is evidence.

    `message` is None for a text that posted nothing - JOIN, STOP, HELP - which
    is logged all the same, because it was billed all the same. `text` is what
    was actually sent, for counting its segments when there is no message to
    read them off; `cost` is the provider's `(amount, currency)`, or None.
    """
    body = text if text is not None else (message.text if message else '')
    amount, currency = cost if cost is not None else (None, '')
    return SmsDelivery.objects.create(
        direction=SmsDelivery.Direction.INBOUND,
        conversation=conversation,
        message=message,
        participant=participant,
        user=participant.user if participant else None,
        counterpart_last4=(number or '')[-4:],
        counterpart_hash=_hash_number(number or ''),
        status=SmsDelivery.Status.RECEIVED,
        segments=segments.count_segments(body)[0],
        provider=provider,
        provider_message_id=provider_message_id,
        cost=amount,
        cost_currency=currency,
    )


def _hash_number(number):
    if not number:
        return ''
    return hashlib.sha256(number.strip().encode()).hexdigest()


# --- spend ceilings --------------------------------------------------------
#
# Reused verbatim from otp/services.py, which already treats a ceiling as
# mandatory for every outbound SMS this product sends. Counting SEGMENTS rather
# than messages, because a long message is several charges.
#
# Fixed windows in the cache, not rolling windows in the database: an attacker
# gaining a few extra segments at a boundary does not matter, and this keeps the
# check to two cache reads on a path that already costs a network call.


def _budget_windows(conversation):
    return [
        (
            f'sms:seg:d:conv:{conversation.pk}',
            86400,
            settings.SMS_MAX_SEGMENTS_PER_GROUP_PER_DAY,
        ),
        ('sms:seg:d:global', 86400, settings.SMS_MAX_SEGMENTS_PER_DAY),
    ]


def _within_budget(conversation, count):
    for key, _ttl, limit in _budget_windows(conversation):
        if (cache.get(key) or 0) + count > limit:
            logger.error('SMS budget reached on %s; not sending', key)
            return False, 'over the daily message budget'
    return True, ''


def _record_spend(conversation, count):
    for key, ttl, _limit in _budget_windows(conversation):
        cache.add(key, 0, ttl)
        try:
            cache.incr(key, count)
        except ValueError:
            # The window expired between the add and the incr. Losing one tick
            # is harmless; failing a send that was already paid for is not.
            cache.set(key, count, ttl)


def dispatch(message_id):
    """Entry point used by `create_message`, after the transaction commits.

    Runs the fan-out on a worker when one is configured, and inline when there
    is not. Inline is honest for a pilot and dangerous at scale - N provider
    calls on the thread that is answering somebody's request - so it is capped,
    loudly, rather than pretending to be fine.
    """
    from chat.models import Message

    message = Message.objects.filter(pk=message_id).select_related(
        'conversation', 'sender_participant'
    ).first()
    if message is None:
        return []
    return fan_out(message)


def schedule(message):
    """Queue this message's fan-out for after the current transaction commits.

    Never inside the transaction: the row must be readable by whoever picks the
    work up, and a provider call inside an open transaction holds a database
    connection open across a network round trip.

    Runs on a Celery worker when a broker is configured, and inline when there
    is not. Inline is honest for a small pilot and wrong at scale - N provider
    calls on the thread answering somebody's request - so it says so in the log
    rather than pretending the two are equivalent.
    """
    if not settings.SMS_SENDING_ENABLED:
        return

    message_id = message.pk
    transaction.on_commit(lambda: _dispatch_somewhere(message_id))


def _dispatch_somewhere(message_id):
    """Hand the fan-out to a worker, or do it here if there is not one.

    Nothing raised in here may escape. This runs from `transaction.on_commit`,
    which Django executes on the request thread AFTER the commit - so an
    exception becomes a 500 on a message that is already saved, already
    broadcast to every app member, and already visible in the conversation. The
    sender is told their message failed while everybody else can read it.

    The SMS leg failing is worth an alarm in the log, not a failed request.
    """
    try:
        _dispatch(message_id)
    except Exception:
        logger.exception('SMS fan-out failed for message %s', message_id)


def _dispatch(message_id):
    if _worker_available():
        from sms.tasks import fan_out_message

        try:
            fan_out_message.delay(message_id)
            return
        except Exception:
            # A broker that is configured but unreachable must not swallow the
            # message. Falling back is strictly better than losing it, and the
            # fan-out is idempotent so a later retry cannot double-send.
            logger.exception('Could not queue fan-out for %s; running inline',
                             message_id)

    dispatch(message_id)


def _worker_available():
    """Whether there is a broker to hand work to. One definition, in loopd/celery.py."""
    from loopd.celery import worker_available

    return worker_available()


def reply_allowed(number):
    """Whether we may answer this number again this hour.

    A reply is a billed message that somebody else triggered by texting us, so
    without a cap anyone who knows a group number can run up a bill by texting
    HELP in a loop. Counted per number, hashed for the same reason the delivery
    log hashes it: cache keys surface in logs and memory dumps.
    """
    target = _hash_number(number)[:32]
    key = f'sms:reply:h:{target}'
    if (cache.get(key) or 0) >= settings.SMS_MAX_REPLIES_PER_NUMBER_PER_HOUR:
        return False
    cache.add(key, 0, 3600)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, 3600)
    return True


# --- one-to-one notices: invitations, approvals, refusals -------------------


def send_reply(conversation, group_number, to_number, text):
    """Answer a text somebody sent us. Guarded exactly like everything else.

    Separate from `send_notice` only because the sender may not be a member -
    a stranger texting HELP gets an answer and has no Participant row - so
    there is nobody to check consent against. Everything else is identical, and
    that matters: a reply is a billed message triggered by an UNAUTHENTICATED
    inbound text, which is the easiest way in the whole product for somebody
    else to spend your money.
    """
    if not SmsSettings.sending_is_on():
        return _record_reply(conversation, to_number, SmsDelivery.Status.SKIPPED,
                             reason='sending is switched off')

    if not reply_allowed(to_number):
        return _record_reply(conversation, to_number, SmsDelivery.Status.SKIPPED,
                             reason='reply cap reached')

    if not is_allowed_destination(to_number):
        return _record_reply(conversation, to_number, SmsDelivery.Status.SKIPPED,
                             reason='destination not allowed')

    count, encoding = segments.count_segments(text)
    allowed, reason = _within_budget(conversation, count)
    if not allowed:
        return _record_reply(conversation, to_number, SmsDelivery.Status.SKIPPED,
                             reason=reason)

    delivery = _record_reply(
        conversation, to_number, SmsDelivery.Status.SENDING,
        segment_count=count, encoding=encoding,
    )
    try:
        result = send_text(to=to_number, from_number=group_number.number, text=text)
    except SmsError as exc:
        delivery.status = SmsDelivery.Status.FAILED
        delivery.reason = str(exc)[:120]
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return delivery

    _record_spend(conversation, count)
    delivery.status = SmsDelivery.Status.SENT
    delivery.provider = result.get('provider', '')
    delivery.provider_message_id = result.get('id') or ''
    delivery.save(
        update_fields=['status', 'provider', 'provider_message_id', 'updated_at']
    )
    return delivery


def _record_reply(conversation, number, status, *, reason='', segment_count=0,
                  encoding=''):
    """A delivery row for a reply to somebody who may not be a member."""
    return SmsDelivery.objects.create(
        direction=SmsDelivery.Direction.OUTBOUND,
        conversation=conversation,
        message=None,
        participant=None,
        user=None,
        counterpart_last4=(number or '')[-4:],
        counterpart_hash=_hash_number(number or ''),
        status=status,
        reason=reason,
        segments=segment_count,
        encoding=encoding,
    )


def send_notice(conversation, participant, text, *, kind='notice'):
    """Text one person about their membership. Returns the SmsDelivery row.

    Every message this product sends to a phone goes through here or through
    `fan_out`, and both obey the same three things: the kill switch, the spend
    ceilings, and the delivery log. A notice that skipped them would be an
    unbounded, unrecorded way to spend money - which is exactly what a "just
    send a quick confirmation" helper turns into.

    `kind='invitation'` is the one case allowed to reach somebody whose consent
    is still PENDING, because the invitation IS the request for consent. Nothing
    reaches somebody who replied STOP, ever.
    """
    number = number_for(participant)
    if not number:
        return None

    consent = membership.consent_status(
        participant, ChannelConsent.Channel.SMS
    )

    if consent in (
        ChannelConsent.Status.OPTED_OUT,
        ChannelConsent.Status.DECLINED,
    ):
        return _record_notice(
            conversation, participant, SmsDelivery.Status.SKIPPED,
            reason='opted out',
        )

    # The delivery switches, as well as the answer. This used to read consent
    # alone, which meant the two could disagree and the notice would believe
    # the wrong one: an admin who switched SMS off for a member left a consent
    # row still saying opted in - and a membership notice went out to somebody who
    # should not have been texted at all.
    if not participant.sms_enabled:
        return _record_notice(
            conversation, participant, SmsDelivery.Status.SKIPPED,
            reason='sms disabled for this group',
        )

    # "Send and receive Loop messages by text", off in the app: a notice about
    # a Loop is a Loop message too.
    if participant.user_id and not MessagingSettings.of(participant.user).sms_enabled:
        return _record_notice(
            conversation, participant, SmsDelivery.Status.SKIPPED,
            reason='sms disabled account-wide',
        )

    if kind != 'invitation' and consent == ChannelConsent.Status.PENDING:
        # They have not answered the invitation yet, so no group traffic and no
        # chatter - only the invitation itself.
        return _record_notice(
            conversation, participant, SmsDelivery.Status.SKIPPED,
            reason='consent not granted',
        )

    if not SmsSettings.sending_is_on():
        return _record_notice(
            conversation, participant, SmsDelivery.Status.SKIPPED,
            reason='sending is switched off',
        )

    if not is_allowed_destination(number):
        return _record_notice(
            conversation, participant, SmsDelivery.Status.SKIPPED,
            reason='destination not allowed',
        )

    count, encoding = segments.count_segments(text)
    allowed, reason = _within_budget(conversation, count)
    if not allowed:
        return _record_notice(
            conversation, participant, SmsDelivery.Status.SKIPPED, reason=reason,
        )

    group_number = GroupNumber.for_number_of(conversation)
    if group_number is None:
        return _record_notice(
            conversation, participant, SmsDelivery.Status.SKIPPED,
            reason='group has no number',
        )

    delivery = _record_notice(
        conversation, participant, SmsDelivery.Status.SENDING,
        segment_count=count, encoding=encoding,
    )
    try:
        result = send_text(to=number, from_number=group_number.number, text=text)
    except SmsError as exc:
        delivery.status = SmsDelivery.Status.FAILED
        delivery.reason = str(exc)[:120]
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return delivery

    _record_spend(conversation, count)
    delivery.status = SmsDelivery.Status.SENT
    delivery.provider = result.get('provider', '')
    delivery.provider_message_id = result.get('id') or ''
    delivery.save(
        update_fields=['status', 'provider', 'provider_message_id', 'updated_at']
    )
    return delivery


def _record_notice(conversation, participant, status, *, reason='',
                   segment_count=0, encoding=''):
    """A delivery row for a message with no Message behind it.

    `message` is null: an invitation is not part of the conversation, it is
    about it. The row still exists so the spend and the outcome are visible.
    """
    number = number_for(participant) or ''
    return SmsDelivery.objects.create(
        direction=SmsDelivery.Direction.OUTBOUND,
        conversation=conversation,
        message=None,
        participant=participant,
        user=participant.user,
        counterpart_last4=number[-4:],
        counterpart_hash=_hash_number(number),
        status=status,
        reason=reason,
        segments=segment_count,
        encoding=encoding,
    )


def retry_delivery(delivery):
    """Send one previously failed delivery again. True if it went out.

    Claimed with a conditional UPDATE before anything is sent, so two workers
    picking up the same row cannot both text the person. `attempts` bounds how
    many times this can ever happen for one row.

    THE ELIGIBILITY CHECK IS RE-RUN, AND HAS TO BE. A retry can fire up to six
    hours after the message was written, and the reason it failed the first
    time - a carrier hiccup - says nothing about whether the person still wants
    it. In that window they can text STOP, mute the group, leave it, or be
    removed from it. Deciding once at fan-out time and trusting it hours later
    is how somebody who has said no gets texted anyway, twice.

    `sms/maintenance.py:PERMANENT_REASONS` does not cover this: it excludes
    rows whose reason was ALREADY a policy refusal, and cannot know about a
    refusal that only became true afterwards.

    The row is marked SKIPPED rather than FAILED when that happens, which is
    both accurate - it is a decision, not a failure - and self-limiting, since
    the sweep never retries a SKIPPED row.
    """
    claimed = SmsDelivery.objects.filter(
        pk=delivery.pk, status=SmsDelivery.Status.FAILED
    ).update(
        status=SmsDelivery.Status.SENDING,
        attempts=delivery.attempts + 1,
        updated_at=timezone.now(),
    )
    if not claimed:
        return False

    delivery.refresh_from_db()
    conversation = delivery.conversation

    # Read fresh, with the consent rows, rather than trusting whatever the
    # sweep's queryset loaded: the point of the check below is that the world
    # has moved since this row was written.
    participant = (
        Participant.objects.filter(pk=delivery.participant_id)
        .select_related('user__profile', 'user__messaging_settings')
        .prefetch_related('channel_consents')
        .first()
    )

    # Read BEFORE the check rather than after, because the check needs it: the
    # message is who sent it, and whether the recipient has blocked them since
    # is one of the things that can have changed in the retry window.
    message = delivery.message

    refusal = still_eligible(participant, message)
    if refusal is not None:
        delivery.status = SmsDelivery.Status.SKIPPED
        delivery.reason = refusal
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return False

    number = number_for(participant)
    group_number = GroupNumber.for_number_of(conversation) if conversation else None

    if not number or group_number is None:
        delivery.status = SmsDelivery.Status.FAILED
        delivery.reason = 'no number to retry to'
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return False

    if not SmsSettings.sending_is_on():
        delivery.status = SmsDelivery.Status.FAILED
        delivery.reason = 'sending is switched off'
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return False

    body = format_message(conversation, message) if message else None
    if body is None:
        delivery.status = SmsDelivery.Status.FAILED
        delivery.reason = 'the message is gone'
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return False

    count, encoding = segments.count_segments(body)
    allowed, reason = _within_budget(conversation, count)
    if not allowed:
        delivery.status = SmsDelivery.Status.FAILED
        delivery.reason = reason
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return False

    try:
        result = send_text(to=number, from_number=group_number.number, text=body)
    except SmsError as exc:
        delivery.status = SmsDelivery.Status.FAILED
        delivery.reason = str(exc)[:120]
        delivery.save(update_fields=['status', 'reason', 'updated_at'])
        return False

    _record_spend(conversation, count)
    delivery.status = SmsDelivery.Status.SENT
    delivery.reason = ''
    delivery.segments = count
    delivery.encoding = encoding
    delivery.provider = result.get('provider', '')
    delivery.provider_message_id = result.get('id') or ''
    delivery.save(update_fields=[
        'status', 'reason', 'segments', 'encoding', 'provider',
        'provider_message_id', 'updated_at',
    ])
    return True
