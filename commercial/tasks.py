"""Celery tasks for campaigns.

Thin on purpose, as in sms/tasks.py: each task is a wrapper with no logic of
its own, so the work stays in commercial/services/ where a plain test calls it
with no broker.
"""
from celery import shared_task

from commercial.services import schedule, settlement, stats


@shared_task(name='commercial.run_campaign_schedule')
def run_campaign_schedule():
    """Start and end campaigns on their dates. Beat runs it every minute."""
    return schedule.run()


@shared_task(name='commercial.settle_and_roll_up')
def settle_and_roll_up():
    """Charge what was seen, then write the day's totals. Every five minutes."""
    settled = settlement.settle_all()
    settled['days_rolled_up'] = stats.roll_up()
    return settled


@shared_task(name='commercial.prune_views')
def prune_views():
    """Per-view rows past the retention period. Daily."""
    return stats.prune()
