"""Charging for what was seen (slice 5).

docs/campaigns_final_plan.md, section 9, "Charging without a hot row". A view
does not touch the company's money when it is recorded; this does, every few
minutes, one ledger row per campaign for all of its views since last time.

A campaign's charge is always billed_views x cpm_cents / 1000, rounded down,
less what it has already been charged - so a price that is a fraction of a
cent per view (US$1 per 1,000) is exact over time, and never more than it
reserved. A view settlement cannot charge (past the reservation) stays
recorded and uncharged.

A campaign whose budget is spent ends (transitions.end), and the rounding
left over returns to available with its release.
"""
import logging

from django.db import transaction
from django.utils import timezone

from commercial.models import AdImpression, Campaign
from commercial.services import counters, pacing, wallet

logger = logging.getLogger(__name__)


def settle_locked(campaign, now=None):
    """Charge `campaign` for its unsettled views. Returns the cents charged.

    The caller holds the campaign's row lock (transitions._lock or
    `settle_all`), so two settlements cannot count the same views. The views
    are named by id, so one recorded meanwhile waits for the next run instead
    of being stamped settled without being counted.
    """
    now = now or timezone.now()
    ids = list(
        AdImpression.objects.filter(campaign=campaign, settled_at__isnull=True).values_list(
            'pk', flat=True
        )
    )
    if not ids:
        return 0
    billable = AdImpression.objects.filter(pk__in=ids, billable=True).count()
    AdImpression.objects.filter(pk__in=ids).update(settled_at=now)
    campaign.billed_views += billable
    campaign.save(update_fields=['billed_views', 'updated_at'])

    charged = 0
    if campaign.cpm_cents:
        owed = campaign.billed_views * campaign.cpm_cents // 1000
        charged = max(0, min(owed - campaign.spent_cents, campaign.reserved_cents))
        if charged:
            wallet.spend(campaign, charged, description=_describe(campaign, billable))

    def forget():
        counters.forget_unsettled(campaign.pk)
        from commercial.services.serving import forget_live

        forget_live()

    transaction.on_commit(forget)
    return charged


def settle_all(now=None):
    """Every campaign with unsettled views; then end the ones now spent.

    {"settled": campaigns, "charged_cents": total, "ended": campaigns}.
    """
    from commercial.services import transitions

    now = now or timezone.now()
    pending = (
        AdImpression.objects.filter(settled_at__isnull=True)
        .values_list('campaign_id', flat=True)
        .distinct()
    )
    settled = charged = ended = 0
    for campaign_id in sorted(set(pending)):
        with transaction.atomic():
            campaign = (
                Campaign.objects.select_for_update().select_related('company').get(pk=campaign_id)
            )
            charged += settle_locked(campaign, now)
        settled += 1
        if spent(campaign):
            try:
                transitions.end(campaign, reason=transitions.BUDGET_SPENT)
            except transitions.TransitionRefused as exc:
                logger.info('Campaign %s did not end: %s', campaign_id, exc)
            else:
                ended += 1
    return {'settled': settled, 'charged_cents': charged, 'ended': ended}


def spent(campaign):
    """Whether a running or paused campaign has billed every view it bought."""
    if campaign.status not in (Campaign.Status.RUNNING, Campaign.Status.PAUSED):
        return False
    return campaign.billed_views >= pacing.views_bought(pacing.snapshot(campaign))


def _describe(campaign, views):
    return f'{views:,} view{"" if views == 1 else "s"} of {campaign}'
