"""How many more views a campaign may have: overall, and today (slice 5).

Counted in views, not money: the price is per 1,000 views, so "money left"
is "views left" at the campaign's own price, and counting views keeps a price
that is a fraction of a cent per view exact.

- Bought: what the whole budget buys, total_budget x 1000 / cpm.
- Left: bought, less the views settlement has billed and those it has not
  settled yet (counters). At nought the campaign stops being served; views
  past it are recorded and never charged (section 9).
- Today: the daily cap in views, if there is one. "Spread evenly" with an
  end date also aims at an equal share of what is left for each day left,
  and spreads that share over the day an hour at a time, so a morning cannot
  take the whole day. "As fast as possible" is held only by the cap.

Both serving (which reads a cached copy, `snapshot`) and measurement (which
reads the campaign row) ask the same questions here.
"""
from datetime import timedelta
from math import ceil

from commercial.models import Campaign
from commercial.services import counters


def snapshot(campaign):
    """What the questions below need, as plain data serving can cache."""
    return {
        'id': campaign.pk,
        'country': campaign.company.country,
        'pacing': campaign.pacing,
        'cpm_cents': campaign.cpm_cents,
        'total_budget_cents': campaign.total_budget_cents or 0,
        'daily_budget_cents': campaign.daily_budget_cents,
        'billed_views': campaign.billed_views,
        'starts_at': campaign.starts_at,
        'ends_at': campaign.ends_at,
    }


def views_bought(row):
    if not row['cpm_cents']:
        return 0
    return row['total_budget_cents'] * 1000 // row['cpm_cents']


def views_left(row):
    return views_bought(row) - row['billed_views'] - counters.unsettled_views(row['id'])


def daily_cap_views(row):
    """The daily cap in views, or None for no cap."""
    if not row['daily_budget_cents'] or not row['cpm_cents']:
        return None
    return row['daily_budget_cents'] * 1000 // row['cpm_cents']


def views_left_today(row, now):
    """How many more views today, given the budget, the cap and the pacing."""
    left = views_left(row)
    if left <= 0:
        return 0
    _day, start, _end = counters.local_day(row['country'], now)
    today = counters.views_today(row['id'], row['country'], now)

    allowance = daily_cap_views(row)
    even = row['pacing'] == Campaign.Pacing.STANDARD
    if even and row['ends_at'] is not None:
        days_left = max(1, ceil((row['ends_at'] - start) / timedelta(days=1)))
        share = ceil((left + today) / days_left)
        allowance = share if allowance is None else min(allowance, share)
    if allowance is None:
        return left
    if even:
        hours = int((now - start) / timedelta(hours=1))
        allowance = ceil(allowance * min(24, hours + 1) / 24)
    return max(0, min(left, allowance - today))
