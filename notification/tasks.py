"""Celery tasks for web push.

Thin wrappers, as mail/tasks.py and sms/tasks.py are: the work lives in
notification/push/outbound.py where a plain test can call it directly, and nothing in the
test suite touches Celery.

NO `autoretry_for`, unlike the mail and SMS fan-outs, and that is the one real
difference. Those are idempotent per recipient - a delivery row records who has
already been sent to, so a retry re-sends to nobody. A push has no such row on
purpose (see notification/push/outbound.py), so a retry would buzz everybody who already got
it a second time. A notification that failed is a notification not worth
repeating: the message is in the app either way.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='push.fan_out_message')
def fan_out_message(message_id):
    from notification.push.outbound import dispatch

    sent = dispatch(message_id)
    logger.info(
        'Pushed message %s to %s browsers', message_id, len(sent)
    )
    return len(sent)


@shared_task(name='push.notification')
def push_notification(notification_id):
    from notification.push.outbound import dispatch_notification

    sent = dispatch_notification(notification_id)
    logger.info(
        'Pushed notification %s to %s browsers', notification_id, len(sent)
    )
    return len(sent)
