"""Who has blocked whom, and what that stops.

One entry point per action, for the reason `chat/membership.py` has one: the
REST endpoints, the direct-conversation serializer and the message write path
all have to ask the same question, and three copies of "or the other way round"
is three chances to write it one-way by accident.

WHAT A BLOCK STOPS, IN THIS RELEASE
-----------------------------------
Direct contact only.

    starting a direct conversation   refused, both directions
    sending into an existing one     refused, both directions
    inviting them to a group         refused
    finding them in user search      they do not appear

Nothing about a group the two people already share changes: both stay members,
both keep seeing each other's messages, and both stay in the member list. The
spec asks for external notifications caused by a blocked member to be
suppressed inside shared groups too - that half is deliberately not built yet,
so `chat/delivery.py` is untouched and the fan-out does not know blocks exist.

READ SYMMETRIC, WRITE ONE-SIDED
-------------------------------
`blocks_between` is true if EITHER person has blocked the other. A block that
only worked in one direction would be detectable: B would notice their own
messages behaving differently from A's, which is precisely what the generic
refusals below exist to prevent.

NOTHING HERE TELLS ANYBODY A BLOCK EXISTS
-----------------------------------------
Callers refuse with whatever they would say if the other person simply were not
there. The refusal copy lives at the call sites, not here, because each one has
a different "nothing to see" to imitate - a search that found nobody, a
username that resolves to nothing, a conversation that will not take a message.
"""
from django.db.models import Q

from user.models import UserBlock


def block(blocker, *, user=None, phone_number=None):
    """Record that `blocker` does not want contact. Idempotent.

    Exactly one of `user` or `phone_number` names the other party, matching the
    model. Blocking twice is one row and not an error: the caller is a person
    pressing a button, and the second press means the same as the first.
    """
    _one_target(user, phone_number)

    if user is not None and user.pk == blocker.pk:
        # The database refuses this too, but a constraint error is a 409 and
        # this is simply a request that means nothing.
        raise ValueError('You cannot block yourself.')

    row, _ = UserBlock.objects.get_or_create(
        blocker=blocker,
        blocked_user=user,
        blocked_phone_number=_normalised(phone_number),
    )
    return row


def unblock(blocker, *, user=None, phone_number=None):
    """Undo it. Returns True if there was anything to undo."""
    _one_target(user, phone_number)

    deleted, _ = UserBlock.objects.filter(
        blocker=blocker,
        blocked_user=user,
        blocked_phone_number=_normalised(phone_number),
    ).delete()
    return bool(deleted)


def blocks_between(one, other):
    """Whether EITHER of these two accounts has blocked the other.

    The question every caller actually has. Both arguments are Users; a caller
    holding a phone number wants `blocks_number` instead.
    """
    if one is None or other is None:
        return False
    if one.pk == other.pk:
        return False

    return UserBlock.objects.filter(
        Q(blocker=one, blocked_user=other) | Q(blocker=other, blocked_user=one)
    ).exists()


def blocks_number(blocker, phone_number):
    """Whether `blocker` has blocked this bare number.

    One direction only, and that is not an oversight: a number with no account
    has nobody who could have done the blocking. When it later becomes an
    account, whether the block follows it is an open product question - see
    docs/loopdn_user_blocking_behavior_spec.md.
    """
    number = _normalised(phone_number)
    if blocker is None or not number:
        return False

    return UserBlock.objects.filter(
        blocker=blocker, blocked_phone_number=number
    ).exists()


def blocked_user_ids(viewer):
    """Every account id in a block relationship with `viewer`, either way.

    For the queryset that hides them from search. Returns ids rather than a
    queryset so the caller can use it in an `__in` without a correlated
    subquery - a person blocks a handful of people, not thousands.
    """
    if viewer is None or not viewer.is_authenticated:
        return []

    rows = UserBlock.objects.filter(
        Q(blocker=viewer) | Q(blocked_user=viewer)
    ).values_list('blocker_id', 'blocked_user_id')

    ids = set()
    for blocker_id, blocked_id in rows:
        ids.add(blocker_id)
        if blocked_id is not None:
            ids.add(blocked_id)
    ids.discard(viewer.pk)
    return list(ids)


def _one_target(user, phone_number):
    if (user is None) == (phone_number is None):
        raise ValueError('Name the other party by exactly one of user or phone_number.')


def _normalised(phone_number):
    """Numbers are stored in one canonical form, so a lookup is exact."""
    return (phone_number or '').strip() or None
