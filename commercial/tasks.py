"""Celery tasks for campaigns.

Thin on purpose, as in sms/tasks.py: each task is a wrapper with no logic of
its own, so the work stays in commercial/services/ where a plain test calls it
with no broker.
"""
from celery import shared_task

from commercial.services import schedule


@shared_task(name='commercial.run_campaign_schedule')
def run_campaign_schedule():
    """Start and end campaigns on their dates. Beat runs it every minute."""
    return schedule.run()
