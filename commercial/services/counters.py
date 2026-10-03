"""The fast counters serving reads (slice 5): a campaign's views today, its
views not yet settled, how many times it was served, and each person's views
for the frequency caps.

docs/campaigns_final_plan.md, sections 9 and 10. In the cache - Redis in
production - as fixed windows, like sms/outbound.py's budgets, so the serving
path never counts rows. The database stays the record: a counter that is
missing (expired, evicted, Redis restarted) is seeded from it on first use, so
losing the cache costs a query, not a wrong answer. `served` is the exception:
nothing records a serve, so it starts again from nought.

Days (decision 13, decision 35): a campaign's day is its company's calendar
day (markets.time_zone) - its daily cap, its pacing, its results, and how
often one person may see it. A person's cap on sponsored cards in all runs on
their own day: the time zone of their verified phone's country, or UTC when
that is unknown - it spans campaigns from companies in different zones, so no
one company's day fits it.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.cache import cache

from commercial.markets import TIME_ZONE, time_zone
from commercial.models import AdImpression
from sms.provisioning import country_for_number

UTC = ZoneInfo('UTC')

#: Longer than a day, so a window outlives the day it counts.
DAY_TTL = 26 * 3600
#: Unsettled views are re-read from the database at least this often.
UNSETTLED_TTL = 3600


def local_day(country, now):
    """(day, start, end) of the calendar day `now` falls in, in `country`."""
    return day_in(time_zone(country), now)


def day_in(zone, now):
    """(day, start, end) of the calendar day `now` falls in, in `zone`."""
    day = now.astimezone(zone).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return day, start, end


def viewer_zone(user):
    """Where a person's day is: their verified phone's country's time zone, or
    UTC. The same evidence `reach.group_country` reads for a group's creator."""
    profile = getattr(user, 'profile', None)
    if profile is None or not profile.phone_no_verified or not profile.phone_number:
        return UTC
    name = TIME_ZONE.get(country_for_number(profile.phone_number))
    return ZoneInfo(name) if name else UTC


# --- reading ------------------------------------------------------------------


def views_today(campaign_id, country, now):
    """Every view of the campaign today, billable or not."""
    day, start, end = local_day(country, now)
    return _read(
        f'campaign:views:{campaign_id}:{day}',
        lambda: AdImpression.objects.filter(
            campaign_id=campaign_id, viewed_at__gte=start, viewed_at__lt=end
        ).count(),
        DAY_TTL,
    )


def unsettled_views(campaign_id):
    """Billable views settlement has not charged yet: spent, not settled."""
    return _read(
        f'campaign:unsettled:{campaign_id}',
        lambda: AdImpression.objects.filter(
            campaign_id=campaign_id, billable=True, settled_at__isnull=True
        ).count(),
        UNSETTLED_TTL,
    )


def served(campaign_id, day):
    return cache.get(_served_key(campaign_id, day)) or 0


def viewer_views(user_id, zone, now):
    """Sponsored cards this person saw today - their day, in `zone` - across
    every campaign."""
    day, start, end = day_in(zone, now)
    return _read(
        _viewer_key(user_id, day),
        lambda: AdImpression.objects.filter(
            participant__user_id=user_id, viewed_at__gte=start, viewed_at__lt=end
        ).count(),
        DAY_TTL,
    )


def viewer_campaign_views(user_id, campaign_id, country, now):
    """This campaign's cards this person saw today - the campaign's day, in its
    company's `country`."""
    day, start, end = local_day(country, now)
    return _read(
        _viewer_campaign_key(user_id, campaign_id, day),
        lambda: AdImpression.objects.filter(
            participant__user_id=user_id,
            campaign_id=campaign_id,
            viewed_at__gte=start,
            viewed_at__lt=end,
        ).count(),
        DAY_TTL,
    )


# --- counting -----------------------------------------------------------------


def count_served(campaign_id, day):
    key = _served_key(campaign_id, day)
    cache.add(key, 0, DAY_TTL)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, DAY_TTL)


def count_view(impression, country, user_id, zone):
    """After an AdImpression is written: every counter it belongs in.
    `country` is the campaign's company's; `zone` the viewer's (viewer_zone)."""
    now = impression.viewed_at
    day, start, end = local_day(country, now)
    campaign_id = impression.campaign_id
    _bump(
        f'campaign:views:{campaign_id}:{day}',
        lambda: AdImpression.objects.filter(
            campaign_id=campaign_id, viewed_at__gte=start, viewed_at__lt=end
        ).count(),
        DAY_TTL,
    )
    if impression.billable:
        _bump(
            f'campaign:unsettled:{campaign_id}',
            lambda: AdImpression.objects.filter(
                campaign_id=campaign_id, billable=True, settled_at__isnull=True
            ).count(),
            UNSETTLED_TTL,
        )
    # A person's caps are only read on serve, so a missing window is left for
    # that read to seed.
    their_day, _start, _end = day_in(zone, now)
    for key in (
        _viewer_key(user_id, their_day),
        _viewer_campaign_key(user_id, campaign_id, day),
    ):
        if cache.get(key) is not None:
            try:
                cache.incr(key)
            except ValueError:
                pass


def forget_unsettled(campaign_id):
    """After settlement: the next read counts again from the database."""
    cache.delete(f'campaign:unsettled:{campaign_id}')


# --- the windows ------------------------------------------------------------


def _served_key(campaign_id, day):
    return f'campaign:served:{campaign_id}:{day}'


def _viewer_key(user_id, day):
    return f'campaign:viewer:{user_id}:{day}'


def _viewer_campaign_key(user_id, campaign_id, day):
    return f'campaign:viewer:{user_id}:{campaign_id}:{day}'


def _read(key, seed, ttl):
    value = cache.get(key)
    if value is None:
        value = seed()
        cache.add(key, value, ttl)
    return value


def _bump(key, seed, ttl):
    """Count one more, after the row is written. A missing window is seeded
    from the database instead - which already counts the new row."""
    if cache.get(key) is None:
        cache.add(key, seed(), ttl)
        return
    try:
        cache.incr(key)
    except ValueError:
        # Expired between the read and the incr.
        cache.set(key, seed(), ttl)
