"""The jobs that keep the SMS layer honest while nobody is watching.

Everything here answers a question the product cannot otherwise answer:

    retry_failed_deliveries    did anybody miss a message because a carrier had
                               a bad minute?
    requeue_stalled_deliveries did a worker die mid-send and leave a row that
                               will sit in SENDING forever?
    provision_missing_numbers  is there a group that never got a number because
                               the provider was down when it was created?
    purge_old_deliveries       is the log growing without limit?
    daily_spend_report         what did yesterday cost, and did anything hit a
                               ceiling?

Plain functions, called by three-line tasks in sms/tasks.py, so every one of
them is testable by calling it - no broker, no beat, no Celery in the test
suite at all.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from chat.models import Conversation
from sms.models import GroupNumber, SmsDelivery, SmsWebhookEvent

logger = logging.getLogger(__name__)

# Reasons that will fail the same way every time. Retrying these is a standing
# charge for a message that is never going to arrive.
PERMANENT_REASONS = (
    'destination not allowed',
    'no verified number',
    'no number',
    'opted out',
    'consent not granted',
    'sms disabled for this group',
    'sms disabled account-wide',
    'muted',
    'group is too large to text',
    'sending is switched off',
)


def retry_failed_deliveries():
    """Try again for people whose message failed on the way out.

    Bounded three ways, because an unbounded retry against a disconnected
    number bills forever: only FAILED rows, only a few attempts each, and only
    recent ones - a message from last week is no longer worth delivering even
    if it now could be.

    SKIPPED rows are never retried. A skip is a decision - muted, opted out,
    over budget - not a failure, and re-deciding it every hour would eventually
    send somebody a message they asked not to receive.
    """
    from sms.outbound import retry_delivery

    cutoff = timezone.now() - timedelta(hours=settings.SMS_RETRY_WINDOW_HOURS)
    stale = (
        SmsDelivery.objects.filter(
            direction=SmsDelivery.Direction.OUTBOUND,
            status=SmsDelivery.Status.FAILED,
            attempts__lt=settings.SMS_MAX_DELIVERY_ATTEMPTS,
            created_at__gte=cutoff,
        )
        .exclude(reason__in=PERMANENT_REASONS)
        # `message__sender_participant` is not decoration: retry_delivery reads
        # the message to rebuild the body AND to ask whether the recipient has
        # blocked its sender since. Without it that is two lazy queries per row,
        # up to four hundred a sweep.
        .select_related(
            'conversation',
            'participant__user__profile',
            'message__sender_participant',
        )[:200]
    )

    retried = 0
    for delivery in stale:
        if retry_delivery(delivery):
            retried += 1
    if retried:
        logger.info('Retried %s failed SMS deliveries', retried)
    return retried


def requeue_stalled_deliveries():
    """Rescue rows left in SENDING by a process that died mid-send.

    A row is marked SENDING before the provider call and moved on after it, so
    anything still SENDING long afterwards belongs to a worker that is gone.
    Left alone it stays there forever: it is not FAILED, so nothing retries it,
    and it is not SENT, so nobody knows the message did not arrive.
    """
    cutoff = timezone.now() - timedelta(minutes=settings.SMS_STALLED_AFTER_MINUTES)
    stalled = SmsDelivery.objects.filter(
        status=SmsDelivery.Status.SENDING, updated_at__lt=cutoff
    )
    count = stalled.update(
        status=SmsDelivery.Status.FAILED,
        reason='no answer from the sender process',
    )
    if count:
        # WARNING rather than INFO: this means something died, and finding out
        # from a delivery report is late.
        logger.warning('Marked %s stalled SMS deliveries as failed', count)
    return count


def provision_missing_numbers():
    """Give a number to any group that never got one.

    A group created while the provider was unreachable is app-only and stays
    that way, because nothing tries again. Its members can talk in the app and
    cannot be reached by text at all, which looks like the SMS feature being
    broken rather than one order having failed.

    Only groups with at least one SMS leg switched on are considered - delivery
    OR replies, matching `provisioning.uses_sms`. A group with both off either
    had them turned off by an admin, was created with both unticked (rule 1c),
    or never qualified for a number at all (rule 1a - its creator has no
    verified number). Buying one for any of those is spending money every month
    on a channel nobody asked for.

    Turning either setting back on is the signal to provision, and this job acts
    on it the next time it runs - so enabling SMS on an old group is a change of
    setting, not a support ticket.
    """
    from sms.provisioning import provision_for

    groups = (
        Conversation.objects.filter(
            Q(sms_delivery_enabled=True) | Q(sms_replies_enabled=True),
            kind=Conversation.Kind.GROUP,
            sms_number=None,
        )
        .select_related('created_by__profile')
        .order_by('created_at')[:settings.SMS_PROVISION_BATCH]
    )

    provisioned = 0
    for group in groups:
        try:
            if provision_for(group, group.created_by) is not None:
                provisioned += 1
        except Exception:
            # One group that cannot be provisioned must not stop the others.
            logger.exception('Could not provision a number for conversation %s', group.pk)
    if provisioned:
        logger.info('Provisioned numbers for %s groups that had none', provisioned)
    return provisioned


def purge_old_deliveries():
    """Drop delivery and webhook records past their retention.

    This table gains a row per recipient per message and never loses one, so
    without this it is the fastest-growing thing in the database - and it holds
    a record of who texts which private group, which is not something to keep
    forever by accident.
    """
    cutoff = timezone.now() - timedelta(days=settings.SMS_RETENTION_DAYS)
    deliveries, _ = SmsDelivery.objects.filter(created_at__lt=cutoff).delete()
    events, _ = SmsWebhookEvent.objects.filter(received_at__lt=cutoff).delete()
    if deliveries or events:
        logger.info('Purged %s deliveries and %s webhook events', deliveries, events)
    return deliveries, events


def daily_spend_report():
    """Email yesterday's SMS spend, and say if anything hit a ceiling.

    The cheapest possible cost alarm: a budget that stops sending does so
    silently, and the first sign otherwise is somebody asking why their group
    went quiet. One mail a day makes both the spend and the silence visible.
    """
    from utils.mail import EmailError, send_email

    since = timezone.now() - timedelta(days=1)
    rows = SmsDelivery.objects.filter(created_at__gte=since)

    totals = rows.aggregate(
        segments=Sum('segments'),
        sent=Count('pk', filter=Q(status=SmsDelivery.Status.SENT)),
        delivered=Count('pk', filter=Q(status=SmsDelivery.Status.DELIVERED)),
        failed=Count('pk', filter=Q(status=SmsDelivery.Status.FAILED)),
        skipped=Count('pk', filter=Q(status=SmsDelivery.Status.SKIPPED)),
        inbound=Count('pk', filter=Q(direction=SmsDelivery.Direction.INBOUND)),
    )

    over_budget = rows.filter(reason__icontains='budget').count()
    switched_off = rows.filter(reason='sending is switched off').count()

    lines = [
        'LoopD SMS, last 24 hours',
        '',
        f'  billed segments   {totals["segments"] or 0}',
        f'  sent              {totals["sent"]}',
        f'  delivered         {totals["delivered"]}',
        f'  failed            {totals["failed"]}',
        f'  skipped           {totals["skipped"]}',
        f'  inbound           {totals["inbound"]}',
        '',
        f'  numbers leased    {GroupNumber.objects.count()}',
        f'  groups with none  '
        f'{Conversation.objects.filter(kind="group", sms_number=None).count()}',
    ]
    if over_budget:
        lines += ['', f'  *** {over_budget} messages were NOT sent: over budget ***']
    if switched_off:
        lines += ['', f'  *** {switched_off} messages were NOT sent: sending is off ***']

    body = '\n'.join(lines)
    logger.info('Daily SMS report:\n%s', body)

    recipient = settings.SMS_REPORT_EMAIL
    if not recipient:
        # No address configured is a decision, not a failure - the report is in
        # the log either way.
        return body

    try:
        send_email(to=recipient, subject='LoopD SMS daily report', html=f'<pre>{body}</pre>',
                   text=body)
    except EmailError:
        logger.exception('Could not email the daily SMS report')
    return body
