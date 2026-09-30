"""Sending a group's messages out over its owner's WhatsApp Business number.

The same shape as sms/outbound.py - schedule after commit, fan out on a worker,
one delivery row per recipient, never let a provider failure reach the message
write path - with the two rules WhatsApp adds:

THE 24-HOUR WINDOW
------------------
Free-form text is only allowed within 24 hours of the recipient's last message
TO this business number. Outside it, Meta refuses anything but an approved
template. So each recipient gets:

    inside the window    the message as text, `Family - Yul: Dinner at 7`
    outside it           the connection's notification template, with the
                         group, the sender and the text as its parameters
    outside, no template SKIPPED, with the reason - never a silent drop

If Meta says the window closed under us (131047) the template is tried instead.

OPT-IN
------
Nobody is messaged without a WHATSAPP ChannelConsent of OPTED_IN - asked by the
shared ladder in chat/delivery.py, as for every channel. The one exception is
the invitation itself, which is the question that consent answers.

WHO PAYS
--------
The group owner, to Meta, on their own WABA. Not us. That is why there are no
spend ceilings here as there are for SMS; there is still a recipient cap per
message, and staff can SUSPEND a connection from the admin to stop it at once.
"""
import hashlib
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from chat import delivery, membership
from chat.models import BlockedFromConversation, ChannelConsent, Participant
from whatsapp import graph
from whatsapp.graph import WhatsAppError
from whatsapp.models import (
    GroupChannel,
    WhatsAppConnection,
    WhatsAppContact,
    WhatsAppDelivery,
)
from whatsapp.onboarding import ConnectionRefused, attach, mark_broken

logger = logging.getLogger(__name__)

WHATSAPP = ChannelConsent.Channel.WHATSAPP
WINDOW = timedelta(hours=24)

# Meta: the recipient's 24-hour window has closed.
REENGAGEMENT_REQUIRED = 131047


def format_message(conversation, message):
    body = (message.text or '').strip() or _attachment_summary(message)
    return f'{conversation.name} - {message.sender_label}: {body}'


def _attachment_summary(message):
    count = message.attachments.count()
    if count == 0:
        return '(no message)'
    if count == 1:
        return "(sent a photo or file - open Loopd'n to see it)"
    return f"(sent {count} photos or files - open Loopd'n to see them)"


_WS = re.compile(r'\s+')


def template_param(value, limit=700):
    """Meta rejects template parameters with newlines, tabs or 4+ spaces in a row."""
    return _WS.sub(' ', str(value or '')).strip()[:limit] or '-'


def wa_id_for(participant):
    """The number to message for this member, as Meta's wa_id (no plus), or None.

    The same address rule as SMS: a phone-only member at the number on their
    row, an account member only at the number they VERIFIED - an unverified
    number is a string somebody typed, and whoever really owns it would get
    this group's messages.
    """
    if participant.user_id is None:
        number = participant.phone_number
    else:
        profile = getattr(participant.user, 'profile', None)
        if profile is None or not profile.phone_no_verified:
            return None
        number = profile.phone_number
    return number.lstrip('+') if number else None


def _address_reason(participant):
    return None if wa_id_for(participant) else 'no verified number'


def still_eligible(participant, message):
    return delivery.retry_refusal(participant, message, WHATSAPP, _address_reason)


def in_window(connection, wa_id):
    last = (
        WhatsAppContact.objects.filter(connection=connection, wa_id=wa_id)
        .values_list('last_inbound_at', flat=True)
        .first()
    )
    return last is not None and timezone.now() - last < WINDOW


# --- the fan-out -------------------------------------------------------------


def fan_out(message):
    """Send one message to every member who should get it on WhatsApp."""
    conversation = message.conversation
    channel = GroupChannel.whatsapp_for(conversation)
    if channel is None:
        # Most groups do not use WhatsApp at all. No rows: a row per member per
        # message for a channel the group never turned on would bury the log.
        return []

    connection = channel.whatsapp_connection
    already = set(
        WhatsAppDelivery.objects.filter(
            message=message, direction=WhatsAppDelivery.Direction.OUTBOUND
        ).values_list('participant_id', flat=True)
    )

    send_to, skipped = delivery.eligible_for(message, WHATSAPP)
    # Never asked is not an omission: they are simply not a WhatsApp member of
    # this group. Logged, it would be one row per app member per message.
    skipped = [(row, reason) for row, reason in skipped if reason != delivery.NO_CONSENT]
    addressable = []
    for row in send_to:
        reason = _address_reason(row)
        if reason is None:
            addressable.append(row)
        else:
            skipped.append((row, reason))

    addressable = [row for row in addressable if row.pk not in already]
    skipped = [(row, reason) for row, reason in skipped if row.pk not in already]

    rows = [
        _record(connection, message, row, WhatsAppDelivery.Status.SKIPPED, reason=reason)
        for row, reason in skipped
    ]

    if not channel.delivery_enabled:
        return rows + [
            _record(connection, message, row, WhatsAppDelivery.Status.SKIPPED,
                    reason='whatsapp is off for this group')
            for row in addressable
        ]

    if len(addressable) > settings.WHATSAPP_MAX_RECIPIENTS_PER_MESSAGE:
        logger.error(
            'Conversation %s has %s WhatsApp recipients, over the cap of %s',
            conversation.pk, len(addressable), settings.WHATSAPP_MAX_RECIPIENTS_PER_MESSAGE,
        )
        return rows + [
            _record(connection, message, row, WhatsAppDelivery.Status.SKIPPED,
                    reason='group is too large for WhatsApp')
            for row in addressable
        ]

    text = format_message(conversation, message)
    token = connection.access_token
    broken = None
    for row in addressable:
        if broken is not None:
            rows.append(_record(connection, message, row, WhatsAppDelivery.Status.FAILED,
                                reason=broken))
            continue
        result = _send_one(connection, token, message, row, text)
        rows.append(result)
        if result.status == WhatsAppDelivery.Status.FAILED and result.reason.startswith('connection:'):
            broken = result.reason
    return rows


def _send_one(connection, token, message, participant, text):
    conversation = message.conversation
    wa_id = wa_id_for(participant)
    use_template = not in_window(connection, wa_id)

    if use_template and not connection.template_name:
        return _record(connection, message, participant, WhatsAppDelivery.Status.SKIPPED,
                       reason='outside the 24-hour window and no template configured')

    row = _record(
        connection, message, participant, WhatsAppDelivery.Status.SENDING,
        kind=WhatsAppDelivery.Kind.TEMPLATE if use_template else WhatsAppDelivery.Kind.TEXT,
    )
    try:
        try:
            result = (
                _send_template(connection, token, wa_id, conversation, message)
                if use_template
                else graph.send_text(connection.phone_number_id, token, to=wa_id, text=text)
            )
        except WhatsAppError as exc:
            if use_template or exc.code != REENGAGEMENT_REQUIRED or not connection.template_name:
                raise
            # Our record of their last message was stale.
            row.kind = WhatsAppDelivery.Kind.TEMPLATE
            result = _send_template(connection, token, wa_id, conversation, message)
    except WhatsAppError as exc:
        row.status = WhatsAppDelivery.Status.FAILED
        if exc.permanent:
            mark_broken(connection, str(exc))
            row.reason = f'connection: {exc}'[:160]
        else:
            row.reason = f'{exc} ({exc.code})'[:160] if exc.code else str(exc)[:160]
        row.save(update_fields=['status', 'reason', 'kind', 'updated_at'])
        return row

    row.status = WhatsAppDelivery.Status.SENT
    row.wamid = result.get('id') or ''
    row.save(update_fields=['status', 'wamid', 'kind', 'updated_at'])

    # A plain reply from them now belongs to the group they just heard from.
    WhatsAppContact.objects.update_or_create(
        connection=connection, wa_id=wa_id,
        defaults={'current_conversation': conversation},
    )
    return row


def _send_template(connection, token, wa_id, conversation, message):
    return graph.send_template(
        connection.phone_number_id, token,
        to=wa_id,
        name=connection.template_name,
        language=connection.template_language,
        parameters=[
            template_param(conversation.name, 60),
            template_param(message.sender_label, 60),
            template_param((message.text or '').strip() or _attachment_summary(message)),
        ],
    )


def _hash(wa_id):
    return hashlib.sha256(f'+{wa_id}'.encode()).hexdigest() if wa_id else ''


def _record(connection, message, participant, status, *, reason='', kind=''):
    wa_id = wa_id_for(participant) or ''
    return WhatsAppDelivery.objects.create(
        direction=WhatsAppDelivery.Direction.OUTBOUND,
        connection=connection,
        conversation=message.conversation if message else participant.conversation,
        message=message,
        participant=participant,
        counterpart_last4=wa_id[-4:],
        counterpart_hash=_hash(wa_id),
        status=status,
        reason=reason[:160],
        kind=kind,
    )


def record_inbound(connection, conversation, participant, message, wa_id, wamid):
    return WhatsAppDelivery.objects.create(
        direction=WhatsAppDelivery.Direction.INBOUND,
        connection=connection,
        conversation=conversation,
        message=message,
        participant=participant,
        counterpart_last4=wa_id[-4:],
        counterpart_hash=_hash(wa_id),
        status=WhatsAppDelivery.Status.RECEIVED,
        wamid=wamid,
    )


def send_reply(connection, wa_id, text, *, reply_to=None):
    """Answer somebody who just wrote to us. Always inside their window."""
    try:
        graph.send_text(connection.phone_number_id, connection.access_token,
                        to=wa_id, text=text, reply_to=reply_to)
    except WhatsAppError as exc:
        if exc.permanent:
            mark_broken(connection, str(exc))
        logger.warning('Could not reply on WhatsApp to ...%s: %s', wa_id[-4:], exc)


# --- scheduling (identical contract to sms/outbound.py) ----------------------


def schedule(message):
    if not settings.WHATSAPP_SENDING_ENABLED:
        return
    message_id = message.pk
    transaction.on_commit(lambda: _dispatch_somewhere(message_id))


def _dispatch_somewhere(message_id):
    try:
        from loopd.celery import worker_available

        if worker_available():
            from whatsapp.tasks import fan_out_message

            try:
                fan_out_message.delay(message_id)
                return
            except Exception:
                logger.exception('Could not queue WhatsApp fan-out for %s; running inline',
                                 message_id)
        dispatch(message_id)
    except Exception:
        logger.exception('WhatsApp fan-out failed for message %s', message_id)


def dispatch(message_id):
    from chat.models import Message

    message = Message.objects.filter(pk=message_id).select_related(
        'conversation', 'sender_participant'
    ).first()
    if message is None:
        return []
    return fan_out(message)


# --- inviting somebody over WhatsApp -----------------------------------------


class InviteResult:
    def __init__(self, participant, channel, outcome, delivery=None):
        self.participant = participant
        self.channel = channel
        # 'invited' | 'already_opted_in' | 'declined' | 'not_sent'
        self.outcome = outcome
        self.delivery = delivery


def channel_for_invite(conversation, by, connection_id=None):
    """The group's WhatsApp channel, attaching the inviter's number if needed.

    This is the "Invite by WhatsApp" entrance: an admin who has connected a
    number but never attached it to this group should not have to do that as a
    separate step. One who has connected nothing is told so, with a code the
    frontend turns into the Embedded Signup flow.
    """
    channel = GroupChannel.whatsapp_for(conversation)
    if channel is not None:
        return channel

    connections = WhatsAppConnection.objects.filter(
        owner=by, status=WhatsAppConnection.Status.ACTIVE
    )
    if connection_id is not None:
        connections = connections.filter(pk=connection_id)
    connections = list(connections[:2])

    if not connections:
        raise ConnectionRefused(
            'Connect a WhatsApp Business number first.', code='whatsapp_not_connected'
        )
    if len(connections) > 1:
        raise ConnectionRefused(
            'Choose which WhatsApp number this group should use.', code='choose_connection'
        )
    return attach(conversation, connections[0], by)


def invite(conversation, by, *, phone_number=None, display_name='', participant=None,
           connection_id=None):
    """Add somebody (or take an existing member) and ask them to opt in on WhatsApp.

    Consent starts PENDING. Nothing from the group reaches them until they tap
    the invitation's button or reply START - an admin typing your number is not
    you agreeing to hear from their business, and Meta's policy says the same.

    The membership is written in a transaction and the invitation sent after it
    commits: a call to Meta must not hold a database transaction open. It is
    sent inline rather than queued so the admin is told straight away whether
    it went - a wrong number is something they can fix while they are looking.
    """
    with transaction.atomic():
        channel = channel_for_invite(conversation, by, connection_id)
        participant = _invitee(conversation, by, participant, phone_number, display_name)

        if wa_id_for(participant) is None:
            raise ConnectionRefused(
                'That member has no verified phone number to reach on WhatsApp.',
                code='no_verified_number',
            )

        answer = membership.consent_status(participant, WHATSAPP)
        if answer == ChannelConsent.Status.OPTED_IN:
            return InviteResult(participant, channel, 'already_opted_in')
        if answer in (ChannelConsent.Status.OPTED_OUT, ChannelConsent.Status.DECLINED):
            # They have answered. Re-inviting is not a new question - only they
            # can change it, by replying START.
            return InviteResult(participant, channel, 'declined')

        membership.set_channel_consent(participant, WHATSAPP, ChannelConsent.Status.PENDING)

    inviter = getattr(getattr(by, 'profile', None), 'display_label', None) or by.get_username()
    row = send_invitation(channel, participant, inviter)
    outcome = 'invited' if row.status == WhatsAppDelivery.Status.SENT else 'not_sent'
    return InviteResult(participant, channel, outcome, row)


def _invitee(conversation, by, participant, phone_number, display_name):
    """The membership being invited, created for a new number.

    The same guards as chat.membership.invite_by_phone - permission to add,
    the block screen, a blocked row - without its SMS invitation.
    """
    if participant is not None:
        if participant.status != Participant.Status.ACTIVE:
            raise membership.NotAMember('That person is not a member of this group.')
        return participant

    if not conversation.may_add_members(by):
        raise membership.JoinRefused('You cannot add people to this group.')
    membership._refuse_blocked_invite(by, phone_number=phone_number)

    existing = membership.resolve(conversation, phone_number=phone_number)
    if existing is not None and existing.status == Participant.Status.BLOCKED:
        raise BlockedFromConversation('That person is blocked from this group.')
    if existing is not None and existing.status == Participant.Status.ACTIVE:
        return existing
    return conversation.add_phone_participant(
        phone_number, display_name=display_name, added_by=by
    )


def send_invitation(channel, participant, inviter):
    """The opt-in request. A template, since the business speaks first."""
    connection = channel.whatsapp_connection
    conversation = channel.conversation
    wa_id = wa_id_for(participant)

    if not connection.invite_template_name:
        if not in_window(connection, wa_id):
            return _record(connection, None, participant, WhatsAppDelivery.Status.SKIPPED,
                           reason='no invitation template configured')
        # They wrote to this number recently, so we may simply ask.
        sender = lambda: graph.send_text(  # noqa: E731
            connection.phone_number_id, connection.access_token, to=wa_id,
            text=f'{inviter} added you to {conversation.name} on Loopd\'n. '
                 'Reply START to get its messages here, or STOP to opt out.',
        )
        kind = WhatsAppDelivery.Kind.TEXT
    else:
        sender = lambda: graph.send_template(  # noqa: E731
            connection.phone_number_id, connection.access_token, to=wa_id,
            name=connection.invite_template_name,
            language=connection.template_language,
            parameters=[template_param(inviter, 60), template_param(conversation.name, 60)],
        )
        kind = WhatsAppDelivery.Kind.TEMPLATE

    row = _record(connection, None, participant, WhatsAppDelivery.Status.SENDING, kind=kind)
    try:
        result = sender()
    except WhatsAppError as exc:
        if exc.permanent:
            mark_broken(connection, str(exc))
        row.status = WhatsAppDelivery.Status.FAILED
        row.reason = str(exc)[:160]
        row.save(update_fields=['status', 'reason', 'updated_at'])
        return row

    row.status = WhatsAppDelivery.Status.SENT
    row.wamid = result.get('id') or ''
    row.save(update_fields=['status', 'wamid', 'updated_at'])
    return row
