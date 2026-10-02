"""Campaigns start and end on their dates (slice 4 of docs/campaigns_final_plan.md).

`run` is what the beat task calls every minute (commercial/tasks.py): it ends
every campaign whose end date has passed, then starts every approved one whose
start date has come - ending first, so a campaign approved after its end date
ends instead of running for a minute.

Each campaign moves in its own transaction, through transitions.start and
transitions.end, so one that can no longer move - its owner cancelled it a
moment ago - is skipped and the rest still move. Two runs at once are
harmless: a transition locks the row and checks the status again, so the
second finds nothing left to do.
"""
import logging

from django.db.models import Q
from django.utils import timezone

from commercial.models import Campaign
from commercial.services import transitions

logger = logging.getLogger(__name__)


def due_to_end(now):
    return Campaign.objects.filter(status__in=transitions.STOPPABLE, ends_at__lte=now)


def due_to_start(now):
    """Approved, with no start date or one that has come, and not already over."""
    return (
        Campaign.objects.filter(status=Campaign.Status.APPROVED)
        .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
        .exclude(ends_at__lte=now)
    )


def run(now=None):
    """End what is over, start what is due. {"ended": n, "started": n}."""
    now = now or timezone.now()
    ended = _each(due_to_end(now), transitions.end)
    started = _each(due_to_start(now), transitions.start)
    return {'ended': ended, 'started': started}


def _each(campaigns, move):
    moved = 0
    for campaign in campaigns.only('pk').order_by('pk'):
        try:
            move(campaign)
        except transitions.TransitionRefused as exc:
            logger.info('Campaign %s did not move: %s', campaign.pk, exc)
            continue
        moved += 1
    return moved
