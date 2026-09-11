"""The jobs that keep the email channel honest while nobody is watching.

The same three questions `sms/maintenance.py` answers, for the same reasons:

    retry_failed_deliveries    did anybody miss a message because the provider
                               had a bad minute?
    requeue_stalled_deliveries did a worker die mid-send and leave a row that
                               will sit in SENDING forever?
    purge_old_deliveries       is the log growing without limit?

Plain functions, called by three-line tasks in mail/tasks.py, so every one of
them is testable by calling it - no broker, no beat, no Celery in the suite.

Until these ran, a failed email was terminal: `fan_out` wrote FAILED and moved
on, and its per-recipient idempotency treated the existence of the row as the
job being done. So one bad minute at the provider meant one person quietly
never got one message, with a row saying so that nothing ever read.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from chat import delivery as delivery_rules
from mail.models import EmailDelivery, EmailWebhookEvent

logger = logging.getLogger(__name__)

#: Reasons that will be the same next hour. The ladder's own decisions come
#: from chat/delivery.py; these are email's, and the provider's.
PERMANENT_REASONS = delivery_rules.POLICY_REASONS + (
    'no verified address',
    'no address',
    'email disabled account-wide',
    'group is too large to email',
    'provider rejected the message',
    'suppressed by the provider',
    'the message is gone',
    'the membership is gone',
)


def retry_failed_deliveries():
    """Try again for people whose email failed on the way out.

    Bounded three ways: only FAILED rows, only a few attempts each, and only
    recent ones - a message from last week is no longer worth delivering even
    if it now could be. SKIPPED, BOUNCED and COMPLAINED rows are never read:
    each is a decision, theirs or the provider's, not a failure.
    """
    from mail.outbound import retry_delivery

    cutoff = timezone.now() - timedelta(hours=settings.MAIL_RETRY_WINDOW_HOURS)
    stale = (
        EmailDelivery.objects.filter(
            direction=EmailDelivery.Direction.OUTBOUND,
            status=EmailDelivery.Status.FAILED,
            attempts__lt=settings.MAIL_MAX_DELIVERY_ATTEMPTS,
            created_at__gte=cutoff,
        )
        .exclude(reason__in=PERMANENT_REASONS)
        # retry_delivery reads the message to rebuild the body and to ask
        # whether the recipient has blocked its sender since; without these
        # that is two lazy queries per row.
        .select_related(
            'conversation',
            'participant__user__profile',
            'message__sender_participant',
        )[:200]
    )

    retried = 0
    for row in stale:
        if retry_delivery(row):
            retried += 1
    if retried:
        logger.info('Retried %s failed email deliveries', retried)
    return retried


def requeue_stalled_deliveries():
    """Rescue rows left in SENDING by a process that died mid-send.

    A row is SENDING between the two writes around the provider call, so
    anything still SENDING long afterwards belongs to a worker that is gone.
    Marked FAILED so the retry sweep picks it up; the provider-side idempotency
    key on the send means a retry of one that actually landed does not become a
    second copy in somebody's inbox.
    """
    cutoff = timezone.now() - timedelta(minutes=settings.MAIL_STALLED_AFTER_MINUTES)
    count = EmailDelivery.objects.filter(
        status=EmailDelivery.Status.SENDING, updated_at__lt=cutoff
    ).update(
        status=EmailDelivery.Status.FAILED,
        reason='no answer from the sender process',
        updated_at=timezone.now(),
    )
    if count:
        # WARNING rather than INFO: something died.
        logger.warning('Marked %s stalled email deliveries as failed', count)
    return count


def purge_old_deliveries():
    """Drop delivery and webhook records past their retention.

    A row per recipient per message, recording who is emailed about which
    private group. Not something to keep forever by accident.
    """
    cutoff = timezone.now() - timedelta(days=settings.MAIL_RETENTION_DAYS)
    deliveries, _ = EmailDelivery.objects.filter(created_at__lt=cutoff).delete()
    events, _ = EmailWebhookEvent.objects.filter(received_at__lt=cutoff).delete()
    if deliveries or events:
        logger.info('Purged %s email deliveries and %s webhook events', deliveries, events)
    return deliveries, events
