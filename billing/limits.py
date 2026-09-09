"""Plan-limit checks the chat app enforces at the point of action.

Deliberately not a permission class: "can this user create a 4th group" needs
a count query scoped to a specific user, which is exactly what a plain
function does with less machinery. `chat` calls these; `billing` never
imports anything from `chat`.
"""
from rest_framework.exceptions import PermissionDenied

from billing.services import get_or_create_subscription


def plan_for(user):
    return get_or_create_subscription(user).plan


def check_can_create_group(user):
    """Raise if creating one more group would put `user` over their plan's cap."""
    from chat.models import Conversation

    plan = plan_for(user)
    if plan.max_groups is None:
        return

    owned = Conversation.objects.filter(
        kind=Conversation.Kind.GROUP, created_by=user
    ).count()
    if owned >= plan.max_groups:
        raise PermissionDenied(
            f'Your {plan.name} plan allows up to {plan.max_groups} groups. '
            'Upgrade your plan to create another.'
        )


def check_can_add_members(conversation, additional_count):
    """Raise if adding `additional_count` people would exceed the group
    creator's plan cap for members-per-group.

    Limits are the creator's, not the caller's: a plan controls what its
    owner's groups may grow to, regardless of which admin happens to be
    adding people today.
    """
    from chat.models import Participant

    plan = plan_for(conversation.created_by)
    if plan.max_members_per_group is None:
        return

    current = conversation.participants.filter(
        status__in=[Participant.Status.ACTIVE, Participant.Status.PENDING]
    ).count()
    if current + additional_count > plan.max_members_per_group:
        raise PermissionDenied(
            f'The {plan.name} plan allows up to {plan.max_members_per_group} '
            'members per group. Upgrade the group owner\'s plan to add more.'
        )
