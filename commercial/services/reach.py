"""Who an audience covers, and how many - as a range, or "too small".

docs/campaigns_final_plan.md, section 7 step 3, and §13 of the product plan: a
company never learns which groups or which people. So an estimate is never an
exact number and never names a group, and below the privacy floors (decision
12) it is no number at all.

`audience_groups` is the one definition of which groups an audience covers.
Reach counts it now, an admin's "why does this group match" (eligibility.py)
asks it, and serving (slice 5) matches against the same rules, so what the
builder promised is what runs.
"""
from django.conf import settings
from django.db.models import Count, Q

from billing.models import Plan, Subscription
from chat.models import Conversation, GroupCategory, Participant
from commercial.markets import region_codes
from commercial.services.errors import Invalid
from sms.provisioning import country_for_number

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

    Every option chosen is saved as none chosen: "any". Ticking every kind of
    group means any kind of group, and every region means the whole country -
    but named choices only match groups that declared one, so without this a
    full set of ticks would quietly match fewer groups than none.
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

    categories = list(categories)
    every_category = set(GroupCategory.objects.filter(active=True).values_list('pk', flat=True))
    if categories and every_category <= {category.pk for category in categories}:
        categories = []
    if cleaned and set(cleaned) == allowed:
        cleaned = []

    return {
        'categories': categories,
        'regions': cleaned,
        'min_group_size': min_group_size,
        'max_group_size': max_group_size,
    }


def audience_groups(country, *, categories=(), regions=(), min_group_size=None, max_group_size=None):
    """The groups an audience covers, each annotated with `member_count`.

    An empty part means "any". A group that has not declared a category
    matches any category, never a named one. "Anywhere" is the company's own
    country, and a group is in a country when it declared a region there, or -
    having declared none - when its creator's verified phone is from there
    (`in_country`). A group whose country cannot be known matches no country's
    "anywhere", so an ad never reaches a group abroad by default.

    Whatever the audience says, a group is never covered when it is under
    MIN_GROUP_SIZE_FOR_ADS or its owner's plan is ad-free (`allows_ads`).
    Size counts every active member, SMS-only ones included: it is a fact
    about the group.
    """
    groups = Conversation.objects.filter(kind=Conversation.Kind.GROUP).filter(allows_ads())

    if categories:
        groups = groups.filter(category__in=categories)

    if regions:
        groups = groups.filter(region__in=regions)
    else:
        groups = groups.filter(in_country(country))

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


def allows_ads():
    """Groups whose owner's plan lets them carry ads (`Plan.shows_ads`).

    The owner is whoever created the group, as for its member cap. Somebody
    with no subscription row yet is on Free (billing.services), so they count
    exactly when Free shows ads.
    """
    allowed = Q(created_by__subscription__plan__shows_ads=True)
    if Plan.objects.filter(code=Plan.Code.FREE, shows_ads=True).exists():
        allowed |= Q(created_by__subscription__isnull=True)
    return allowed


def viewer_sees_ads(user):
    """Whether this person may be shown ads at all (decision 14): their own
    plan shows them. Somebody with no subscription row yet is on Free.

    Read-only on purpose - billing's plan_for creates the row, and serving
    writes nothing.
    """
    shows = (
        Subscription.objects.filter(user_id=user.pk).values_list('plan__shows_ads', flat=True).first()
    )
    if shows is None:
        return Plan.objects.filter(code=Plan.Code.FREE, shows_ads=True).exists()
    return shows


def group_country(group):
    """The country a group is in, by the same rule as `in_country`: the one
    its declared region is in, else its creator's verified phone's, else None.
    """
    if group.region:
        return group.region.split('-', 1)[0]
    profile = getattr(group.created_by, 'profile', None)
    if profile is None or not profile.phone_no_verified or not profile.phone_number:
        return None
    return country_for_number(profile.phone_number)


def in_country(country):
    """Groups in `country`: it, or one of its regions, declared - or, with
    none declared, a creator whose verified phone is from there."""
    return (
        Q(region=country)
        | Q(region__startswith=f'{country}-')
        | (Q(region='') & creator_in(country))
    )


def creator_in(country):
    """Groups whose creator's verified phone is from `country`.

    The rule that already decides which country a group's SMS number comes
    from (sms/provisioning.country_for), read from the same prefix table - but
    not SMS_FORCE_COUNTRY, which is about where numbers can be bought, not
    where anybody is. Longest prefix wins there (country_for_number, which
    `group_country` uses), so a longer prefix mapped to another country is
    excluded here.
    """
    table = settings.SMS_COUNTRY_BY_PREFIX
    mine = [prefix for prefix, code in table.items() if code == country]
    if not mine:
        return Q(pk__in=[])
    theirs = [
        prefix
        for prefix, code in table.items()
        if code != country and any(prefix.startswith(own) for own in mine)
    ]

    phone = 'created_by__profile__phone_number'
    matches = Q(created_by__profile__phone_no_verified=True)
    any_mine = Q()
    for prefix in mine:
        any_mine |= Q(**{f'{phone}__startswith': prefix})
    matches &= any_mine
    for prefix in theirs:
        matches &= ~Q(**{f'{phone}__startswith': prefix})
    return matches


def as_range(count):
    """{"low": 100, "high": 250}: low <= count < high. `high` is null past the top."""
    low = max((step for step in REACH_STEPS if step <= count), default=0)
    high = next((step for step in REACH_STEPS if step > count), None)
    return {'low': low, 'high': high}


def _too_small():
    # No number at all, not even a lower bound: "fewer than 5 groups" is a count.
    return {'available': False, 'reason': TOO_SMALL}
