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
