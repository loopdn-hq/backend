"""Celery tasks for WhatsApp. Thin wrappers, as in sms/tasks.py."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name='whatsapp.fan_out_message',
    # Idempotent per recipient (one outbound row per message per member), so a
    # redelivery after a worker dies does not message anybody twice.
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
    retry_jitter=True,
)
def fan_out_message(message_id):
    from whatsapp.outbound import dispatch

    rows = dispatch(message_id)
    logger.info('WhatsApp fan-out for message %s: %s rows', message_id, len(rows))
    return len(rows)
