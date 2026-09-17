"""Celery tasks for the email channel.

Thin on purpose, exactly as `sms/tasks.py` is: every task here is a wrapper
with no logic of its own, so the work stays in `mail/outbound.py` where a plain
test can call it directly. Nothing in the test suite touches Celery.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name='mail.fan_out_message',
    # The fan-out is idempotent per recipient, so a redelivery after a worker
    # dies re-sends to nobody.
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
    retry_jitter=True,
)
def fan_out_message(message_id):
    from mail.outbound import dispatch

    rows = dispatch(message_id)
    logger.info('Emailed message %s to %s recipients', message_id, len(rows))
    return len(rows)


# --- the scheduled jobs ----------------------------------------------------
#
# Wrappers, exactly as sms/tasks.py has them. The work is in mail/maintenance.py
# so a test can call it directly.


@shared_task(name='mail.retry_failed_deliveries')
def retry_failed_deliveries():
    from mail.maintenance import retry_failed_deliveries as run

    return run()


@shared_task(name='mail.requeue_stalled_deliveries')
def requeue_stalled_deliveries():
    from mail.maintenance import requeue_stalled_deliveries as run

    return run()


@shared_task(name='mail.announce_failed_deliveries')
def announce_failed_deliveries():
    from mail.maintenance import announce_failed_deliveries as run

    return run()


@shared_task(name='mail.purge_old_deliveries')
def purge_old_deliveries():
    from mail.maintenance import purge_old_deliveries as run

    return run()
