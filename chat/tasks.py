"""Celery tasks for chat.

Thin on purpose, as sms/tasks.py and mail/tasks.py are: the work lives in
chat/link_previews.py, where a plain test can call it without a broker.
"""
from celery import shared_task


@shared_task(name="chat.refresh_link_preview", ignore_result=True)
def refresh_link_preview(message_id):
    # Not retried: a page that failed once gets no card, and the message is
    # already delivered - retrying would only fetch a stranger's page again.
    from chat.link_previews import refresh_preview

    refresh_preview(message_id)
