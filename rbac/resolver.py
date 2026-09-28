"""`can()`, and the two functions built on it.

Nothing in this module is wired into the app yet - step 4 is what replaces the
checks in `chat/` with calls to `can()`. Until then this exists to be TESTED
against the behaviour it will replace, which is the whole point of landing it
in its own step.

TWO QUESTIONS, BOTH MUST PASS
-----------------------------
    can(user, 'group.member.add', conversation)
      1. does a role this user holds allow it?   RoleFeature - the floor
      2. do the subject's own settings allow it? rbac/policy.py

The floor is data seeded from `rbac/registry.py`; the second layer is code,
because "whether `add_member_permission` is MEMBERS" is not a fact about a role.

WHAT THIS IS NOT
----------------
Not access control. `CLAUDE.md` is explicit that access control is the queryset:
`ConversationViewSet.get_queryset()` starts from `participants__user=...`, so a
non-member gets 404 and cannot learn a conversation exists. This layer sits
INSIDE that boundary and answers "what may this member do here". A bug here is
a wrong menu, not a data leak - and it must stay that way.
"""
from django.core.cache import cache
from rest_framework.exceptions import PermissionDenied

from rbac import policy, scopes
from rbac.models import Feature, RoleFeature

#: How long a grants lookup is reused. The generation below makes a change
#: immediate in the process that made it; this bounds how long ANOTHER worker
#: can keep serving the old answer, because the default cache is per-process.
_GRANTS_TTL = 60
_GENERATION_KEY = 'rbac:grants:generation'


def _generation():
    value = cache.get(_GENERATION_KEY)
    if value is None:
        value = 1
        cache.set(_GENERATION_KEY, value, None)
    return value


def invalidate_grants():
    """Forget every cached grants lookup. Called when the matrix changes.

    WHAT IS AND IS NOT CACHED, because the difference decides how bad a stale
    read can be:

    - A USER'S ROLES are never cached. Revoking somebody's `admin` takes effect
      on their next request, everywhere, immediately. That is the operation
      that matters in an incident.
    - The ROLE-TO-FEATURE grants are cached. Those change only when an admin
      edits the permissions matrix, which is rare and deliberate, and the worst
      case is another worker serving the previous answer for up to a minute.

    CALLED BY A SIGNAL, WHICH `QuerySet.update()` DOES NOT FIRE.
    `RoleFeature.objects.filter(...).update(allowed=False)` changes the rows and
    leaves this cache holding the old answer until the TTL expires. Write
    through `rbac/services.py:set_grant`, which saves the instance - it is also
    the only path with the locked-feature guards and the audit trail on it.
    """
    try:
        cache.incr(_GENERATION_KEY)
    except ValueError:
        # The key expired between the read and the incr, or was never set.
        cache.set(_GENERATION_KEY, 1, None)


def _allowed_keys(roles, axis):
    """Every feature key these roles are granted on this axis.

    Cached, because this runs once per conversation on a list page and the
    answer depends only on (roles, axis) - a handful of combinations across the
    whole product. Without it the conversation list costs a query per row, and
    a test in chat/tests.py asserts it does not.
    """
    if not roles:
        return set()

    key = f'rbac:grants:{_generation()}:{axis}:{",".join(sorted(roles))}'
    cached = cache.get(key)
    if cached is not None:
        return set(cached)

    found = set(
        RoleFeature.objects.filter(
            role__in=roles, allowed=True, feature__axis=axis
        ).values_list('feature__key', flat=True)
    )
    cache.set(key, sorted(found), _GRANTS_TTL)
    return found


def _decide(roles, feature_key, subject, axis, user=None):
    """The whole answer, once the roles are known.

    Both entry points funnel through here so "what a grant means" is described
    once: the floor allows it, AND the subject's own settings allow it.
    """
    if not roles:
        return False

    if feature_key not in _allowed_keys(roles, axis):
        return False

    return policy.allows(user, feature_key, subject, roles=roles)


def can(user, feature_key, subject=None):
    """May this user do this, to this thing?

    `subject` selects the axis: None is the account axis, a Conversation is the
    conversation axis. An unknown feature key is False, never an exception - a
    typo in a call site must fail closed. `rbac/checks.py` is what catches the
    typo at startup, which is the right place to be loud about it.
    """
    axis = scopes.axis_for(subject)
    roles = scopes.roles_for(user, subject)
    return _decide(roles, feature_key, subject, axis, user=user)


def can_participant(participant, feature_key, conversation=None):
    """The same question, asked of a MEMBERSHIP rather than an account.

    The conversation axis is really about memberships; a User is only one way
    to find one. Two paths need this rather than `can()`:

    - Somebody who joined by texting JOIN has a Participant and NO account at
      all. `can()` would find no roles and refuse them, which would silently
      stop inbound SMS reaching the group.
    - `create_message` already holds the row. Re-deriving it from the user
      would be a second query on the single write path for every message.

    `conversation` is accepted so a caller that already has it does not pay for
    `participant.conversation`. ACTIVE is required here as it is in
    `scopes.conversation_roles`: a pending, departed, removed or blocked row is
    still a row, and reading a role off one would let somebody who was removed
    keep what they had.
    """
    from chat.models import Participant

    if participant is None:
        return False
    if participant.status != Participant.Status.ACTIVE:
        return False

    subject = conversation if conversation is not None else participant.conversation
    return _decide(
        {participant.role}, feature_key, subject, 'conversation',
        user=participant.user,
    )


def features_for(user, subject=None):
    """Every feature key this user may use on this subject, right now.

    The set a client renders menus from.
    """
    axis = scopes.axis_for(subject)
    roles = scopes.roles_for(user, subject)
    return features_for_roles(roles, subject, axis, user=user)


def features_for_roles(roles, subject, axis, user=None):
    """`features_for`, for a caller that already knows the roles.

    The conversation serializer does: it walks the prefetched participants to
    render `is_admin` anyway, so re-deriving the role here would be a query per
    row on a page that lists twenty conversations.
    """
    if not roles:
        return set()

    keys = _allowed_keys(roles, axis)
    narrowed = set(policy.narrowed_keys()) & keys
    for key in narrowed:
        if not policy.allows(user, key, subject, roles=roles):
            keys.discard(key)
    return keys


def require(user, feature_key, subject=None, message=None):
    """`can()`, but raise instead of returning False.

    Raises DRF's PermissionDenied, which `loopd/exceptions.py` already funnels
    into the {code, message, data} envelope - so a call site needs no handling
    of its own.
    """
    if can(user, feature_key, subject):
        return
    raise PermissionDenied(message or _refusal(feature_key))


def _refusal(feature_key):
    """The human sentence for a refusal, from the feature's own label.

    Read from the catalogue rather than written at each call site, so the
    wording of a refusal and the wording on the permissions screen cannot
    drift apart.
    """
    label = (
        Feature.objects.filter(key=feature_key)
        .values_list('label', flat=True)
        .first()
    )
    if label:
        return f'You do not have permission to: {label.lower()}.'
    return 'You do not have permission to do that.'
