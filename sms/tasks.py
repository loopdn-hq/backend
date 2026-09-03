"""Celery tasks for SMS.

Thin on purpose: every task here is a wrapper with no logic of its own, so the
work stays in `sms/outbound.py` where a plain test can call it directly. Nothing
in the test suite touches Celery, no broker is contacted, and
CELERY_TASK_ALWAYS_EAGER is never set - that split is the whole reason for this
file existing separately.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name='sms.fan_out_message',
    # The fan-out is idempotent (it returns early when delivery rows already
    # exist), so a redelivery after a worker dies is safe rather than a second
    # round of billed messages.
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
    # A task that has failed four times is not going to succeed on the fifth,
    # and each attempt costs real messages for the recipients it did reach.
    retry_jitter=True,
)
def fan_out_message(message_id):
    from sms.outbound import dispatch

    rows = dispatch(message_id)
    logger.info('Fanned out message %s to %s recipients', message_id, len(rows))
    return len(rows)


# --- the scheduled jobs ----------------------------------------------------
#
# Every one is a wrapper. The work lives in sms/maintenance.py so a test can
# call it directly - no broker, no beat, no Celery anywhere in the test suite.


@shared_task(name='sms.retry_failed_deliveries')
def retry_failed_deliveries():
    from sms.maintenance import retry_failed_deliveries as run

    return run()


@shared_task(name='sms.requeue_stalled_deliveries')
def requeue_stalled_deliveries():
    from sms.maintenance import requeue_stalled_deliveries as run

    return run()


@shared_task(name='sms.provision_missing_numbers')
def provision_missing_numbers():
    from sms.maintenance import provision_missing_numbers as run

    return run()


@shared_task(name='sms.purge_old_deliveries')
def purge_old_deliveries():
    from sms.maintenance import purge_old_deliveries as run

    return run()


@shared_task(name='sms.daily_spend_report')
def daily_spend_report():
    from sms.maintenance import daily_spend_report as run

    return run()
