"""Who an audience covers, and how many - as a range, or "too small".

docs/campaigns_final_plan.md, section 7 step 3, and §13 of the product plan: a
company never learns which groups or which people. So an estimate is never an
exact number and never names a group, and below the privacy floors (decision
12) it is no number at all.

`audience_groups` is the one definition of which groups an audience covers.
Reach counts it now; serving (slice 5) matches against the same rules, so what
the builder promised is what runs.
"""
from django.conf import settings
from django.db.models import Count, Q

from chat.models import Conversation, Participant
from commercial.markets import region_codes
from commercial.services.errors import Invalid

TOO_SMALL = 'Audience is too small.'

#: The edges an estimate is rounded to: it is always "between two of these".
REACH_STEPS = (
    1, 5, 10, 25, 50, 100, 250, 500,
    1_000, 2_500, 5_000, 10_000, 25_000, 50_000,
    100_000, 250_000, 500_000, 1_000_000,
)


def clean_audience(company, *, categories=(), regions=(), min_group_size=None, max_group_size=None):
    """The audience as given, checked and normalised for `company`.

    Regions must be in the company's own country - the market it advertises
    in - and are upper-cased and de-duplicated. Raises Invalid otherwise.
    """
    allowed = region_codes(company.country)
    cleaned = []
    for code in regions:
        code = (code or '').strip().upper()
        if code not in allowed:
            raise Invalid({
                'regions': f'"{code}" is not a region of {company.get_country_display()}.'
            })
        if code not in cleaned:
            cleaned.append(code)

    if (
        min_group_size is not None
        and max_group_size is not None
        and max_group_size < min_group_size
    ):
        raise Invalid({
            'max_group_size': "The largest group size can't be below the smallest.",
        })

    return {
        'categories': list(categories),
        'regions': cleaned,
        'min_group_size': min_group_size,
        'max_group_size': max_group_size,
    }


def audience_groups(country, *, categories=(), regions=(), min_group_size=None, max_group_size=None):
    """The groups an audience covers, each annotated with `member_count`.

    An empty part means "any", and "any" includes a group that has not
    declared it: an undeclared group matches a campaign that targets any
    category or anywhere, and never one that names a category or a region.
    "Anywhere" is the company's own country.

    A group under MIN_GROUP_SIZE_FOR_ADS is never covered, whatever the
    audience says. Size counts every active member, SMS-only ones included:
    it is a fact about the group.
    """
    groups = Conversation.objects.filter(kind=Conversation.Kind.GROUP)

    if categories:
        groups = groups.filter(category__in=categories)

    if regions:
        groups = groups.filter(region__in=regions)
    else:
        groups = groups.filter(
            Q(region='') | Q(region=country) | Q(region__startswith=f'{country}-')
        )

    groups = groups.annotate(
        member_count=Count(
            'participants',
            filter=Q(participants__status=Participant.Status.ACTIVE),
            distinct=True,
        )
    ).filter(member_count__gte=max(settings.MIN_GROUP_SIZE_FOR_ADS, min_group_size or 0))

    if max_group_size is not None:
        groups = groups.filter(member_count__lte=max_group_size)
    return groups


def estimate(country, **audience):
    """{"available": true, "groups": range, "members": range}, or "too small".

    `members` is the people who could see an ad in chat: distinct accounts
    among the active members. An SMS-only member never opens the chat app, so
    counting them would promise views that cannot happen.
    """
    groups = audience_groups(country, **audience)

    group_count = groups.count()
    if group_count < settings.MIN_ELIGIBLE_GROUPS:
        return _too_small()

    member_count = (
        Participant.objects.filter(
            conversation__in=groups.values('pk'),
            status=Participant.Status.ACTIVE,
            user__isnull=False,
        )
        .values('user_id')
        .distinct()
        .count()
    )
    if member_count < settings.MIN_ELIGIBLE_MEMBERS:
        return _too_small()

    return {
        'available': True,
        'groups': as_range(group_count),
        'members': as_range(member_count),
    }


def estimate_for(targeting):
    """The estimate for a campaign's saved audience."""
    return estimate(
        targeting.campaign.company.country,
        categories=list(targeting.categories.all()),
        regions=targeting.regions,
        min_group_size=targeting.min_group_size,
        max_group_size=targeting.max_group_size,
    )


def as_range(count):
    """{"low": 100, "high": 250}: low <= count < high. `high` is null past the top."""
    low = max((step for step in REACH_STEPS if step <= count), default=0)
    high = next((step for step in REACH_STEPS if step > count), None)
    return {'low': low, 'high': high}


def _too_small():
    # No number at all, not even a lower bound: "fewer than 5 groups" is a count.
    return {'available': False, 'reason': TOO_SMALL}
