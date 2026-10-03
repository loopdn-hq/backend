"""Does a group match a campaign's audience - and if not, why not.

For an admin answering "why isn't this ad showing in that group" (slice 4 of
docs/campaigns_final_plan.md). It walks the audience one rule at a time and
names each, but the verdict is reach.audience_groups' own answer for that
group, so the explanation can never disagree with what reach counts and
serving (slice 5) runs. It reads; it writes nothing.

Never for a company: which groups a campaign could reach is the membership
disclosure §13 forbids. Only platform_admin calls this.
"""
from dataclasses import dataclass

from django.conf import settings

from billing.models import Subscription
from chat.models import Conversation, Participant
from commercial.services import reach
from utils import regions


@dataclass(frozen=True)
class Check:
    code: str
    label: str
    passed: bool
    detail: str


def explain(campaign, group):
    """{"matches": bool, "member_count": n, "checks": [Check, ...]} for one group.

    `matches` is about the audience only. Whether the campaign is running,
    funded and inside its dates is the campaign's own status, not the group's.
    """
    targeting = getattr(campaign, 'targeting', None)
    audience = {
        'categories': list(targeting.categories.all()) if targeting else [],
        'regions': list(targeting.regions) if targeting else [],
        'min_group_size': targeting.min_group_size if targeting else None,
        'max_group_size': targeting.max_group_size if targeting else None,
    }
    country = campaign.company.country
    this_group = Conversation.objects.filter(pk=group.pk)
    members = group.participants.filter(status=Participant.Status.ACTIVE).count()

    checks = [
        _allows_ads(group, this_group),
        _big_enough(members),
        _category(group, audience['categories']),
        _place(group, this_group, country, audience['regions']),
        _size(members, audience['min_group_size'], audience['max_group_size']),
    ]
    matches = reach.audience_groups(country, **audience).filter(pk=group.pk).exists()
    return {'matches': matches, 'member_count': members, 'checks': checks}


def _allows_ads(group, this_group):
    subscription = (
        Subscription.objects.filter(user_id=group.created_by_id).select_related('plan').first()
    )
    plan = subscription.plan.name if subscription else 'Free, with no subscription yet'
    return Check(
        'allows_ads',
        "Its owner's plan allows ads",
        this_group.filter(reach.allows_ads()).exists(),
        f"{group.created_by.username}'s plan: {plan}.",
    )


def _big_enough(members):
    floor = settings.MIN_GROUP_SIZE_FOR_ADS
    return Check(
        'size_floor',
        f'At least {floor} active members, for privacy',
        members >= floor,
        f'It has {members}.',
    )


def _category(group, categories):
    declared = f'It is "{group.category.label}".' if group.category_id else (
        "It hasn't said what kind of group it is."
    )
    if not categories:
        return Check('category', 'Any kind of group', True, declared)
    return Check(
        'category',
        'One of: ' + ', '.join(category.label for category in categories),
        group.category_id in {category.pk for category in categories},
        declared,
    )


def _place(group, this_group, country, chosen):
    if group.region:
        declared = f'It says it is in {regions.region_name(group.region)}.'
    elif this_group.filter(reach.creator_in(country)).exists():
        declared = (
            "It hasn't said where it is; its creator's verified phone is from "
            f'{regions.region_name(country)}, so it counts as there.'
        )
    else:
        declared = (
            "It hasn't said where it is, and its creator's phone doesn't place "
            f'it in {regions.region_name(country)}.'
        )
    if not chosen:
        return Check(
            'place',
            f'Anywhere in {regions.region_name(country)}',
            this_group.filter(reach.in_country(country)).exists(),
            declared,
        )
    return Check(
        'place',
        'In ' + '; '.join(regions.region_name(code) for code in chosen),
        group.region in chosen,
        declared,
    )


def _size(members, smallest, largest):
    if smallest is None and largest is None:
        return Check('size', 'Any size', True, f'It has {members} active members.')
    if largest is None:
        label = f'{smallest} members or more'
    elif smallest is None:
        label = f'Up to {largest} members'
    else:
        label = f'{smallest} to {largest} members'
    passed = (smallest is None or members >= smallest) and (largest is None or members <= largest)
    return Check('size', label, passed, f'It has {members} active members.')
