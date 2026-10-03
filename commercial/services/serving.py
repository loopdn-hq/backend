"""Which sponsored card, if any, one person sees in one group, now (slice 5).

docs/campaigns_final_plan.md, section 10: a per-viewer slot, not a message.
The checks, in order, stopping at the first "no":

    the switch (AdSettings)  ->  the placement exists  ->  the group may carry
    ads (its owner's plan, its size)  ->  the viewer may see ads (their plan,
    decision 14)  ->  the viewer's cap on cards in all, on their own day  ->
    running campaigns whose audience matches the group  ->  inside their
    dates  ->  budget left today (pacing.py)  ->  the viewer's cap for that
    campaign, on its company's day  ->  pick one (most left today, jittered;
    no auction)  ->  one of its approved creatives

It writes NOTHING to the database. What it served is signed into two tokens
(commercial/tokens.py): one the app reports the view with - a row is written
only then (measurement.py) - and one behind the button, /c/{token}/
(clicks.py). The running campaigns and each group's facts are
cached for a minute, so a serve is a few cache reads and one creative.

Matching here is the same rule as reach.audience_groups - checked against it
in commercial/tests.py - so what the builder promised is what runs.
"""
import random
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db.models import Prefetch
from django.utils import timezone

from chat.models import Conversation, Participant
from commercial.models import AdSettings, Campaign, Creative
from commercial.services import counters, pacing, reach
from commercial.tokens import issue_click, issue_delivery, new_nonce

LIVE_KEY = 'campaign:live'
LIVE_TTL = 60
GROUP_TTL = 60
VIEWER_TTL = 60


@dataclass(frozen=True)
class Served:
    token: str
    click_token: str
    creative: Creative


def get_ad(viewer, conversation, placement=Campaign.Placement.CHAT, now=None):
    """A Served card for `viewer` in `conversation`, or None.

    The caller has checked `viewer` is an active member of the group.
    """
    now = now or timezone.now()
    if not AdSettings.serving_is_on():
        return None
    if placement not in Campaign.Placement.values:
        return None
    group = group_facts(conversation)
    if not group['carries_ads']:
        return None
    if not _viewer_sees_ads(viewer):
        return None
    zone = _viewer_zone(viewer)
    if counters.viewer_views(viewer.pk, zone, now) >= settings.SPONSORED_VIEWS_PER_DAY:
        return None

    best = None
    for row in live_campaigns():
        if row['placement'] != placement or not matches(row, group) or not in_dates(row, now):
            continue
        today = pacing.views_left_today(row, now)
        if today <= 0:
            continue
        seen = counters.viewer_campaign_views(viewer.pk, row['id'], row['country'], now)
        if seen >= settings.SPONSORED_VIEWS_PER_CAMPAIGN_PER_DAY:
            continue
        weight = today * random.uniform(0.8, 1.2)
        if best is None or weight > best[0]:
            best = (weight, row)
    if best is None:
        return None

    row = best[1]
    creative = (
        Creative.objects.filter(
            pk=random.choice(row['creatives']), status=Creative.Status.APPROVED
        )
        .select_related('campaign__company')
        .first()
    )
    if creative is None:
        return None

    day, _start, _end = counters.local_day(row['country'], now)
    counters.count_served(row['id'], day)
    facts = {
        'nonce': new_nonce(),
        'campaign_id': row['id'],
        'creative_id': creative.pk,
        'conversation_id': conversation.pk,
        'viewer_id': viewer.pk,
        'placement': placement,
    }
    return Served(
        token=issue_delivery(**facts), click_token=issue_click(**facts), creative=creative
    )


def matches(row, group):
    """Whether a campaign's audience covers a group: reach's rule, in Python."""
    if row['categories'] and group['category_id'] not in row['categories']:
        return False
    if row['regions']:
        if group['region'] not in row['regions']:
            return False
    elif group['country'] != row['country']:
        return False
    if row['min_group_size'] is not None and group['members'] < row['min_group_size']:
        return False
    if row['max_group_size'] is not None and group['members'] > row['max_group_size']:
        return False
    return True


def in_dates(row, now):
    return (row['starts_at'] is None or row['starts_at'] <= now) and (
        row['ends_at'] is None or now < row['ends_at']
    )


# --- cached facts ---------------------------------------------------------------


def live_campaigns():
    """Every running campaign, as plain data, refreshed every minute."""
    rows = cache.get(LIVE_KEY)
    if rows is None:
        rows = _load_live()
        cache.set(LIVE_KEY, rows, LIVE_TTL)
    return rows


def forget_live():
    """After something the cached copy holds has changed: settlement, a status."""
    cache.delete(LIVE_KEY)


def group_facts(conversation):
    """What matching needs to know about a group, cached for a minute."""
    key = f'campaign:group:{conversation.pk}'
    facts = cache.get(key)
    if facts is None:
        facts = _group_facts(conversation)
        cache.set(key, facts, GROUP_TTL)
    return facts


def _load_live():
    campaigns = (
        Campaign.objects.filter(status=Campaign.Status.RUNNING, cpm_cents__isnull=False)
        .select_related('company', 'targeting')
        .prefetch_related(
            'targeting__categories',
            Prefetch(
                'creatives',
                queryset=Creative.objects.filter(status=Creative.Status.APPROVED).only(
                    'pk', 'campaign_id'
                ),
            ),
        )
    )
    rows = []
    for campaign in campaigns:
        creatives = [creative.pk for creative in campaign.creatives.all()]
        targeting = getattr(campaign, 'targeting', None)
        if not creatives:
            continue
        row = pacing.snapshot(campaign)
        row.update(
            placement=campaign.placement,
            creatives=creatives,
            categories=(
                [category.pk for category in targeting.categories.all()] if targeting else []
            ),
            regions=list(targeting.regions) if targeting else [],
            min_group_size=targeting.min_group_size if targeting else None,
            max_group_size=targeting.max_group_size if targeting else None,
        )
        rows.append(row)
    return rows


def _group_facts(conversation):
    if conversation.kind != Conversation.Kind.GROUP:
        return {'carries_ads': False}
    group = (
        Conversation.objects.filter(pk=conversation.pk)
        .select_related('created_by__profile')
        .first()
    )
    if group is None:
        return {'carries_ads': False}
    members = group.participants.filter(status=Participant.Status.ACTIVE).count()
    allowed = Conversation.objects.filter(pk=group.pk).filter(reach.allows_ads()).exists()
    return {
        'carries_ads': allowed and members >= settings.MIN_GROUP_SIZE_FOR_ADS,
        'category_id': group.category_id,
        'region': group.region,
        'country': reach.group_country(group),
        'members': members,
    }


def _viewer_zone(viewer):
    """The person's time zone, for their cap on cards in all; cached."""
    key = f'campaign:viewer-zone:{viewer.pk}'
    name = cache.get(key)
    if name is None:
        name = counters.viewer_zone(viewer).key
        cache.set(key, name, VIEWER_TTL)
    return ZoneInfo(name)


def _viewer_sees_ads(viewer):
    key = f'campaign:viewer-plan:{viewer.pk}'
    sees = cache.get(key)
    if sees is None:
        sees = reach.viewer_sees_ads(viewer)
        cache.set(key, sees, VIEWER_TTL)
    return sees
