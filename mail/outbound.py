"""Sending a group's messages out over email.

The same shape as `sms/outbound.py`, and deliberately so - one message in, many
sends out, every omission written down. What differs is what each channel is
for, and it is worth saying plainly because it decides most of the code below.

SMS reaches somebody who has nothing installed. Email reaches somebody who has
an account and is not looking at it. So this path is cheap where the SMS one is
expensive, and careful where the SMS one is careless: an email costs a
fraction of a cent, and the real cost of getting it wrong is a person marking
the product as spam, which is a domain-wide injury and not a per-message one.

WHAT THIS WILL NOT DO
---------------------
Send to the person who wrote the message. Send to somebody who muted the group,
declined email for it, or turned email off account-wide. Send to an address
nobody has verified - which is the whole gate for email, exactly as a verified
number is for SMS, because an unverified address is a string somebody typed and
mailing it publishes a private group to whoever really owns it.

Each of those is a row in `EmailDelivery` with a reason.

And it will not mail a direct message at all. That is decided once for every
channel in `chat/delivery.py:conversation_reason`, not here.
"""
import hashlib
import logging
from email.utils import formataddr

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from chat import delivery
from chat.models import ChannelConsent, Participant
from mail import commands
from mail.models import EmailDelivery
from mail.tokens import reply_address, unsubscribe_url
from user.models import MessagingSettings
from utils.mail import GROUP_MESSAGE, EmailError, send_templated_email

logger = logging.getLogger(__name__)


def from_header(conversation):
    """`Family via Loopd <no-reply@loopdn.com>`.

    The address stays ours because it has to be on a domain the provider has
    verified; only the name in front of it says which group this is. Built with
    `formataddr` rather than an f-string so a group called `Smith, Jane` is
    quoted instead of parsed as two recipients.

    The name is collapsed to a single line first. A group name is user input
    and nothing upstream forbids a newline in it, which would be header
    injection - and `utils.mail` rightly refuses to send such a From at all, so
    without this one group's name would silently break every email it ever
    sent, permanently and for everybody in it.
    """
    name = ' '.join((conversation.name or '').split())
    label = f'{name} via {settings.EMAIL_PRODUCT_NAME}'
    return formataddr((label, settings.DEFAULT_FROM_EMAIL))


def list_id_for(conversation):
    """`<group-312.lists.loopdn.com>` - RFC 2919, and it earns its place.

    THE PROBLEM IT SOLVES. A mail client's own unsubscribe control keys on the
    SENDER, and `from_header` puts every group behind one address with only the
    display name changing. So Gmail can offer "Unsubscribe from
    no-reply@loopdn.com?", the member presses it believing everything stops, we
    stop one group, mail keeps arriving from the others - and THAT is what
    produces a spam complaint, which is the one event that really does suppress
    an address account-wide at the provider.

    List-ID says "this message belongs to one specific list", which is the
    standards-blessed way to tell a client that these groups are not one
    stream. The eventual right answer is a per-group From address, which
    changes deliverability posture and needs catch-all handling; this gets most
    of the benefit for one header.

    Deliberately no human-readable phrase part. A group name is user input, a
    `<` or `>` in it would break the header, and `utils/mail.py` only rejects
    newlines. The id form is machine-stable and has no injection surface.
    """
    domain = (settings.DEFAULT_FROM_EMAIL or '').rpartition('@')[2] or 'loopdn.com'
    return f'<group-{conversation.pk}.lists.{domain}>'


def body_for(message):
    """What the recipient reads. Attachments are described, never attached.

    One line, because the rule moved to `Message.preview_text` when push needed
    the same sentence on a lock screen. Kept as a name here: this module's
    other functions read as a set, and `body_for(message)` beside
    `address_for(participant)` says what it is for.
    """
    return message.preview_text


def address_for(participant):
    """The address to mail for this member, or None."""
    if participant.user_id is None:
        return None
    return (participant.user.email or '').strip() or None


def recipients_for(message):
    """Who should receive this message as an email, and who should not.

    Returns (send_to, skipped) where `skipped` carries a reason per person, so
    the delivery log can answer for its own omissions.
    """
    send_to, skipped = delivery.eligible_for(
        message, ChannelConsent.Channel.EMAIL
    )

    # The shared ladder settled membership, muting and consent. Whether there
    # is an address we may write to is email's own question, and is asked here.
    mailable, no_address = [], []
    for row in send_to:
        reason = _address_reason(row)
        if reason is None:
            mailable.append(row)
        else:
            no_address.append((row, reason))

    return mailable, skipped + no_address


def still_eligible(participant, message):
    """Why this member must not be emailed RIGHT NOW, or None if they may be.

    The email twin of `sms/outbound.py:still_eligible`, and one line for the
    same reason: the composition - conversation, ladder, block, address - lives
    once in `chat/delivery.py:retry_refusal`, so a retry cannot be re-decided
    differently on the two channels. `message` is required, not defaulted;
    that docstring says why.
    """
    return delivery.retry_refusal(
        participant, message, ChannelConsent.Channel.EMAIL, _address_reason
    )


def _address_reason(participant):
    """Why there is no address we may mail, or None if there is one.

    Only the address question - membership, muting and consent were settled by
    chat.delivery before this is reached.
    """
    if participant.user_id is None:
        # A phone row, or an invitation to an address nobody has claimed yet.
        # An unclaimed invitation is answered by verifying it, and until that
        # happens they are in no group they can see - so there is nothing to
        # send them about it.
        return 'no verified address'

    profile = getattr(participant.user, 'profile', None)
    if profile is None or not MessagingSettings.of(participant.user).email_enabled:
        return 'email disabled account-wide'
    if not profile.email_verified:
        return 'no verified address'
    if not address_for(participant):
        return 'no address'
    return None


def fan_out(message):
    """Send one message to everybody who should get it as an email.

    Returns the EmailDelivery rows it created. Safe to call with no provider
    configured: `utils.mail` logs instead of sending, so the whole decision
    path still runs and is still recorded.
    """
    conversation = message.conversation

    # Idempotent PER RECIPIENT rather than per message, for the reason
    # sms/outbound.py gives at length: a dispatch that died halfway must not
    # re-send to the people it reached, and must still finish the job for the
    # ones it did not.
    already = set(
        EmailDelivery.objects.filter(
            message=message, direction=EmailDelivery.Direction.OUTBOUND
        ).values_list('participant_id', flat=True)
    )

    send_to, skipped = recipients_for(message)

    if already:
        send_to = [row for row in send_to if row.pk not in already]
        skipped = [pair for pair in skipped if pair[0].pk not in already]
        if not send_to and not skipped:
            logger.info('Email fan-out for message %s already done', message.pk)
            return []

    rows = [
        _record(message, row, EmailDelivery.Status.SKIPPED, reason=reason)
        for row, reason in skipped
    ]

    if len(send_to) > settings.MAIL_MAX_RECIPIENTS_PER_MESSAGE:
        # Everybody still has it in the app; only the email leg stops. A group
        # this large mailing every message is how a sending domain earns a
        # reputation it cannot spend its way out of.
        logger.error(
            'Conversation %s has %s email recipients, over the cap of %s; not sending',
            conversation.pk, len(send_to), settings.MAIL_MAX_RECIPIENTS_PER_MESSAGE,
        )
        return rows + [
            _record(message, row, EmailDelivery.Status.SKIPPED,
                    reason='group is too large to email')
            for row in send_to
        ]

    sender = from_header(conversation)
    body = body_for(message)

    for row in send_to:
        rows.append(_send_one(message, row, sender, body))
    return rows


def _send_one(message, participant, sender, body):
    record = _record(
        message, participant, EmailDelivery.Status.SENDING, attempts=1
    )
    return _deliver(record, message, participant, sender, body)


def retry_delivery(delivery_row):
    """Send one previously failed delivery again. True if it went out.

    Claimed with a conditional UPDATE before anything is sent, so two workers
    picking up the same row cannot both mail the person. `attempts` bounds how
    many times this can ever happen for one row.

    The eligibility check is re-run, and has to be - `still_eligible` says
    why. A refusal marks the row SKIPPED rather than FAILED: it is a decision,
    not a failure, and the sweep never reads a SKIPPED row, so the question is
    not asked again next hour.
    """
    claimed = EmailDelivery.objects.filter(
        pk=delivery_row.pk, status=EmailDelivery.Status.FAILED
    ).update(
        status=EmailDelivery.Status.SENDING,
        attempts=delivery_row.attempts + 1,
        updated_at=timezone.now(),
    )
    if not claimed:
        return False

    delivery_row.refresh_from_db()

    # Read fresh, with the consent rows, rather than trusting whatever the
    # sweep's queryset loaded: the point of the check below is that the world
    # has moved since this row was written.
    participant = (
        Participant.objects.filter(pk=delivery_row.participant_id)
        .select_related('user__profile', 'user__messaging_settings')
        .prefetch_related('channel_consents')
        .first()
    )
    message = delivery_row.message

    refusal = still_eligible(participant, message)
    if refusal is not None:
        _finish(delivery_row, EmailDelivery.Status.SKIPPED, refusal)
        return False

    if message is None:
        _finish(delivery_row, EmailDelivery.Status.FAILED, 'the message is gone')
        return False

    record = _deliver(
        delivery_row, message, participant,
        from_header(message.conversation), body_for(message),
    )
    return record.status == EmailDelivery.Status.SENT


def _deliver(record, message, participant, sender, body):
    """One provider call for one row, and the outcome written on the row.

    Shared by the first send and by `retry_delivery`, so a failure is recorded
    the same way whichever path met it. The reason is one of two fixed strings
    rather than the provider's text: `mail/maintenance.py` reads it to decide
    whether trying again could help, and the provider's wording is in the log.
    """
    address = address_for(participant)
    reply_to = reply_address(participant)
    stop_url = unsubscribe_url(participant)

    try:
        message_id = send_templated_email(
            GROUP_MESSAGE,
            to=address,
            context={
                'group_name': message.conversation.name,
                'sender_name': message.sender_label,
                'body': body,
                # False on a deployment with no inbound email, so the template
                # does not invite a reply that would land nowhere.
                'can_reply': bool(reply_to),
                # The way out, in every single message. None when no public API
                # URL is configured, which the templates branch on - and which
                # mail/checks.py refuses to let a real deployment do.
                'unsubscribe_url': stop_url,
                'stop_keyword': commands.KEYWORD,
            },
            from_email=sender,
            reply_to=reply_to,
            # RFC 3834. It tells the recipient's mail system that a human did
            # not type this particular send, which is the signal that stops a
            # vacation responder answering it - and an out-of-office reply to a
            # group's reply address is posted into the group as a message,
            # fanned back out to everybody, and answered again tomorrow.
            #
            # It does not close the loop on its own: a responder that ignores
            # the header still gets through, and catching that needs the
            # headers off the inbound event, which waits on the real provider
            # payload. This is the half that costs nothing.
            headers=_headers_for(message.conversation, stop_url),
            # One send per recipient per message, enforced at the provider as
            # well as by the constraint above - a retry that got as far as
            # Resend must not become a second copy in somebody's inbox.
            idempotency_key=f'msg-{message.pk}-p-{participant.pk}',
        )
    except EmailError as exc:
        # One bad address must not stop the other nine people getting the
        # message, so this is caught per recipient and never re-raised.
        _finish(
            record,
            EmailDelivery.Status.FAILED,
            'provider rejected the message' if exc.permanent else 'provider unavailable',
        )
        return record

    record.status = EmailDelivery.Status.SENT
    record.reason = ''
    record.provider = 'resend' if message_id else ''
    record.provider_message_id = message_id or ''
    record.save(update_fields=[
        'status', 'reason', 'provider', 'provider_message_id', 'updated_at',
    ])
    return record


def _finish(record, status, reason):
    record.status = status
    record.reason = reason[:120]
    record.save(update_fields=['status', 'reason', 'updated_at'])


def _headers_for(conversation, stop_url):
    """The headers that only another mail system reads.

    `List-Unsubscribe` and its `-Post` partner are RFC 8058 one-click: together
    they are what makes a mail client render its own unsubscribe control and
    POST to us when it is pressed. Gmail and Yahoo's bulk-sender rules expect
    them, and a missing pair measurably drives complaints - which is the exact
    outcome this whole path exists to avoid.

    The URL is OURS. Never the provider's unsubscribe machinery, never its
    merge token: those are account-scoped by construction, so a click on one
    really would stop all of this product's mail to that address, including the
    password reset somebody needs to get back into their own account. Ours
    touches one ChannelConsent row.

    No `mailto:` alternative. `mail/inbound.py` reads the body, and a client
    that sends an empty-bodied mailto would be answered by silence - a way out
    that quietly does nothing is worse than not offering it. The https form is
    what the bulk-sender rules require anyway.
    """
    headers = {
        # RFC 3834: a human did not type this particular send, which is the
        # signal that stops a vacation responder answering it.
        'Auto-Submitted': 'auto-generated',
        'List-ID': list_id_for(conversation),
    }
    if stop_url:
        headers['List-Unsubscribe'] = f'<{stop_url}>'
        headers['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
    return headers


def _record(message, participant, status, *, reason='', attempts=0):
    address = address_for(participant) or ''
    return EmailDelivery.objects.create(
        direction=EmailDelivery.Direction.OUTBOUND,
        conversation=message.conversation,
        message=message,
        participant=participant,
        user=participant.user,
        counterpart_domain=_domain(address),
        counterpart_hash=_hash_address(address),
        status=status,
        reason=reason,
        attempts=attempts,
    )


def record_inbound(conversation, participant, message, address, provider=''):
    """Log a reply that arrived. Evidence, and the other half of the story."""
    return EmailDelivery.objects.create(
        direction=EmailDelivery.Direction.INBOUND,
        conversation=conversation,
        message=message,
        participant=participant,
        user=participant.user if participant else None,
        counterpart_domain=_domain(address or ''),
        counterpart_hash=_hash_address(address or ''),
        status=EmailDelivery.Status.RECEIVED,
        provider=provider,
    )


def _domain(address):
    _, _, domain = (address or '').rpartition('@')
    return domain[:64]


def _hash_address(address):
    """Addresses are stored hashed - see EmailDelivery's docstring.

    Case-folded first: `Alice@x.com` and `alice@x.com` are one mailbox, and
    hashing them to two values would make the log useless for the one question
    it exists to answer.
    """
    if not address:
        return ''
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


# --- getting the work onto a worker ----------------------------------------


def dispatch(message_id):
    """Run one message's fan-out. The entry point the Celery task calls."""
    from chat.models import Message

    message = Message.objects.filter(pk=message_id).select_related(
        'conversation', 'sender_participant'
    ).first()
    if message is None:
        return []
    return fan_out(message)


def schedule(message):
    """Queue this message's fan-out for after the current transaction commits.

    Registered as the `email` channel in mail/apps.py, so this is called for
    every message the product stores. Never inside the transaction: the row has
    to be readable by whoever picks the work up, and a provider call inside an
    open transaction holds a database connection across a network round trip.
    """
    if not settings.MAIL_SENDING_ENABLED:
        return

    message_id = message.pk
    transaction.on_commit(lambda: _dispatch_somewhere(message_id))


def _dispatch_somewhere(message_id):
    """Hand the fan-out to a worker, or do it here if there is not one.

    Nothing raised in here may escape. It runs from `transaction.on_commit`,
    which Django executes on the request thread after the commit - so an
    exception becomes a 500 on a message that is already saved, already
    broadcast, and already readable by everybody else in the group.
    """
    try:
        _dispatch(message_id)
    except Exception:
        logger.exception('Email fan-out failed for message %s', message_id)


def _dispatch(message_id):
    if _worker_available():
        from mail.tasks import fan_out_message

        try:
            fan_out_message.delay(message_id)
            return
        except Exception:
            # A broker that is configured but unreachable must not swallow the
            # message. The fan-out is idempotent, so running it here cannot
            # double-send even if the queued copy later arrives.
            logger.exception('Could not queue email fan-out for %s; running inline',
                             message_id)

    dispatch(message_id)


def _worker_available():
    # One definition, in loopd/celery.py. Kept as a function here because
    # tests patch it by this name.
    from loopd.celery import worker_available

    return worker_available()
