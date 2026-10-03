"""A campaign's days in totals, and the reports read from them (slice 5).

docs/campaigns_final_plan.md, section 11. `roll_up` writes CampaignStatsDaily
from the raw rows every few minutes - today and yesterday, so the last views
before midnight land in their own day - and reports read only those rows,
never AdImpression. `prune` deletes per-view rows past the retention period
(decision 15) once they are settled; the daily totals stay.

What a company sees is aggregates only, and reach - a count of people -
below MIN_ELIGIBLE_MEMBERS is given as null, "too few to show", never as a
number: a reach of 2 in a day narrows down to people. Views and spend are
always shown; they are what the company pays for. Clicks (slice 6) are the
counted ones only (services/clicks.py), on the day they were pressed.

Results (slice 7): what each objective asked for - a website campaign's are
its clicks, a lead campaign's its leads (AdConversion rows), each with the
cost of one. `result` names which, so the dashboard can say "leads".
"""
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from commercial.markets import time_zone
from commercial.models import (
    AdClick,
    AdConversion,
    AdImpression,
    AdTransaction,
    Campaign,
    CampaignStatsDaily,
)
from commercial.services import counters


def roll_up(now=None):
    """Today's and yesterday's totals for every campaign that had any. Returns
    how many day rows were written."""
    now = now or timezone.now()
    since = now - timedelta(days=2)
    viewed = AdImpression.objects.filter(viewed_at__gte=since).values('campaign_id')
    clicked = AdClick.objects.filter(clicked_at__gte=since).values('impression__campaign_id')
    converted = AdConversion.objects.filter(occurred_at__gte=since).values('campaign_id')
    campaigns = Campaign.objects.filter(
        Q(pk__in=viewed)
        | Q(pk__in=clicked)
        | Q(pk__in=converted)
        | Q(status__in=[Campaign.Status.RUNNING, Campaign.Status.PAUSED])
    ).select_related('company')
    written = 0
    for campaign in campaigns:
        today = now.astimezone(time_zone(campaign.company.country)).date()
        for day in (today - timedelta(days=1), today):
            written += _roll_day(campaign, day)
    return written


def prune(now=None):
    """Delete settled per-view rows older than the retention period."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.SPONSORED_VIEW_RETENTION_DAYS)
    deleted, _ = AdImpression.objects.filter(
        viewed_at__lt=cutoff, settled_at__isnull=False
    ).delete()
    return deleted


def campaign_report(campaign):
    """{"totals": {...}, "days": [...]} for one campaign."""
    rows = list(campaign.daily_stats.order_by('day'))
    viewed = sum(row.viewed for row in rows)
    clicks = sum(row.clicks for row in rows)
    kind = result_of(campaign)
    results = sum(_results(row, kind) for row in rows)
    return {
        'currency': campaign.company.currency,
        'reach_shown_from': settings.MIN_ELIGIBLE_MEMBERS,
        'result': kind,
        'totals': {
            'served': sum(row.served for row in rows),
            'viewed': viewed,
            'reach': _shown(rows[-1].reach_to_date) if rows else None,
            'clicks': clicks,
            'results': results,
            'spend_cents': campaign.spent_cents,
            **_costs(campaign.spent_cents, viewed, clicks, results),
        },
        'days': [
            {
                'day': row.day,
                'served': row.served,
                'viewed': row.viewed,
                'reach': _shown(row.reach),
                'clicks': row.clicks,
                'results': _results(row, kind),
                'spend_cents': row.spend_cents,
            }
            for row in rows
        ],
    }


#: What counts as a result, by objective (section 11). A booking campaign,
#: from before bookings were hidden, can only have counted clicks.
RESULT_OF = {
    Campaign.Objective.WEBSITE: 'clicks',
    Campaign.Objective.BOOKING: 'clicks',
    Campaign.Objective.LEAD: 'leads',
    Campaign.Objective.GROUP_JOIN: 'joins',
    Campaign.Objective.CALL: 'calls',
    Campaign.Objective.TEXT: 'texts',
}


def result_of(campaign):
    """What a campaign's results are called: 'clicks', 'leads', 'joins'..."""
    if campaign.objective == Campaign.Objective.LEAD and not _has_form(campaign):
        # Submitted before the lead form, with a link button: clicks.
        return 'clicks'
    return RESULT_OF[campaign.objective]


def _has_form(campaign):
    return campaign.creatives.filter(cta_kind='lead_form').exists()


def _results(row, kind):
    return row.clicks if kind == 'clicks' else row.results


def company_report(company):
    """Every campaign of `company`, by day and by campaign. Reach is per
    campaign only: people do not add up across campaigns."""
    campaigns = list(
        company.campaigns.exclude(status=Campaign.Status.DRAFT)
        .annotate(
            viewed=Sum('daily_stats__viewed'),
            served=Sum('daily_stats__served'),
            clicks=Sum('daily_stats__clicks'),
            result_count=Sum('daily_stats__results'),
        )
        .order_by('-created_at', '-pk')
    )
    reach = _latest_reach(company)
    days = (
        CampaignStatsDaily.objects.filter(campaign__company=company)
        .values('day')
        .annotate(
            served=Sum('served'),
            viewed=Sum('viewed'),
            clicks=Sum('clicks'),
            spend_cents=Sum('spend_cents'),
            campaigns=Count('campaign', distinct=True),
        )
        .order_by('day')
    )
    viewed = sum(c.viewed or 0 for c in campaigns)
    clicks = sum(c.clicks or 0 for c in campaigns)
    spend = sum(c.spent_cents for c in campaigns)
    return {
        'currency': company.currency,
        'reach_shown_from': settings.MIN_ELIGIBLE_MEMBERS,
        'totals': {
            'served': sum(c.served or 0 for c in campaigns),
            'viewed': viewed,
            'clicks': clicks,
            'spend_cents': spend,
            **_costs(spend, viewed, clicks),
        },
        'days': list(days),
        'campaigns': [
            {
                'id': c.pk,
                'name': c.name,
                'status': c.status,
                'objective': c.objective,
                'viewed': c.viewed or 0,
                'clicks': c.clicks or 0,
                'reach': _shown(reach.get(c.pk, 0)),
                'spend_cents': c.spent_cents,
                **_campaign_results(c),
            }
            for c in campaigns
        ],
    }


def _campaign_results(campaign):
    kind = result_of(campaign)
    results = (campaign.clicks if kind == 'clicks' else campaign.result_count) or 0
    return {
        'result': kind,
        'results': results,
        'cost_per_result_cents': campaign.spent_cents // results if results else None,
    }


def _latest_reach(company):
    """{campaign id: people reached to date}, from each campaign's last day."""
    latest = {}
    for campaign_id, reach in (
        CampaignStatsDaily.objects.filter(campaign__company=company)
        .order_by('campaign_id', 'day')
        .values_list('campaign_id', 'reach_to_date')
    ):
        latest[campaign_id] = reach
    return latest


def _costs(spend, viewed, clicks, results=None):
    """What a company reads off its spend: per 1,000 views, per click, and -
    for one campaign - per result."""
    costs = {
        'cost_per_1000_views_cents': spend * 1000 // viewed if viewed else None,
        'cost_per_click_cents': spend // clicks if clicks else None,
    }
    if results is not None:
        costs['cost_per_result_cents'] = spend // results if results else None
    return costs


def _shown(people):
    return people if people >= settings.MIN_ELIGIBLE_MEMBERS else None


def _roll_day(campaign, day):
    zone = time_zone(campaign.company.country)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)

    views = AdImpression.objects.filter(campaign=campaign, viewed_at__gte=start, viewed_at__lt=end)
    viewed = views.count()
    served = counters.served(campaign.pk, day)
    spend = -(
        AdTransaction.objects.filter(
            campaign=campaign,
            kind=AdTransaction.Kind.CAMPAIGN_SPEND,
            created_at__gte=start,
            created_at__lt=end,
        ).aggregate(total=Sum('reserved_change_cents'))['total']
        or 0
    )
    clicks = AdClick.objects.filter(
        impression__campaign=campaign, counted=True, clicked_at__gte=start, clicked_at__lt=end
    ).count()
    results = AdConversion.objects.filter(
        campaign=campaign, occurred_at__gte=start, occurred_at__lt=end
    ).count()
    row = CampaignStatsDaily.objects.filter(campaign=campaign, day=day).first()
    if row is None and not (viewed or served or spend or clicks or results):
        return 0

    people = 'participant__user_id'
    reach = views.exclude(participant=None).values(people).distinct().count()
    reach_to_date = (
        AdImpression.objects.filter(campaign=campaign, viewed_at__lt=end)
        .exclude(participant=None)
        .values(people)
        .distinct()
        .count()
    )
    row = row or CampaignStatsDaily(campaign=campaign, day=day)
    # The served counter lives in the cache and can be lost; never go back.
    row.served = max(row.served, served)
    row.viewed = viewed
    row.reach = reach
    row.reach_to_date = max(row.reach_to_date, reach_to_date)
    row.clicks = clicks
    row.results = results
    row.spend_cents = spend
    row.save()
    return 1
