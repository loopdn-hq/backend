"""The second layer: what the SUBJECT'S OWN settings allow.

A `RoleFeature` row is the floor - what the role permits in principle. Some
features are then narrowed by the conversation's own settings, which admins
change in chat settings and members see in user settings. `can()` asks both,
and both must pass.

The floor therefore has to stay OPEN for anything a setting can loosen. If
`group.member.add` were denied to `member` at the role level,
`add_member_permission = MEMBERS` could never grant it and the setting would be
inert. That is why three conversation features are seeded True for `member`.

A rule here may only NARROW. It never widens, because widening would let a bug
in this module grant something the permissions matrix says is denied - and the
matrix is what people review. If a rule wants to say yes where the floor says
no, the floor is wrong, or the question is not a role question at all.

DIRECT CONVERSATIONS ARE NOT A ROLE QUESTION
--------------------------------------------
Both sides of a one-to-one are MEMBER; there are no admins to grant anything to.
So "may I pin in a DM?" is really "am I one of the two people?" - a MEMBERSHIP
test, which `Conversation.may_pin` already asks as a separate early return
before any role is consulted.

It stays there. Pulling it in here would mean either widening (forbidden above)
or seeding `group.message.pin` as allowed for `member`, which would make the
matrix read "members may pin" - false for every group, and the matrix is the
artifact people trust.
"""
from chat.models import Conversation

#: Features whose answer depends on something beyond the role. Anything not
#: named here is settled by the floor alone.
_CONVERSATION_RULES = {}


def _rule(key):
    def register(fn):
        _CONVERSATION_RULES[key] = fn
        return fn
    return register


@_rule('group.message.send')
def _may_send(user, conversation, is_admin):
    """Broadcast narrows posting to admins. Chat mode allows everybody.

    Mirrors `Conversation.allows_posting_by`, which is the single write path's
    check and stays the enforcement - this is the same rule, asked in advance
    so a client can grey out the composer rather than discover it on send.
    """
    if conversation.mode != Conversation.Mode.BROADCAST:
        return True
    return is_admin


@_rule('group.member.add')
def _may_add_members(user, conversation, is_admin):
    """`add_member_permission` may open this to every member."""
    if conversation.add_member_permission == Conversation.MemberAddPermission.MEMBERS:
        return True
    return is_admin


@_rule('group.settings.edit')
def _may_edit_group(user, conversation, is_admin):
    """`edit_group_permission` may open name/photo/description to members.

    Never covers the policy settings - those are `group.settings.policy`, which
    has no rule here and so is settled by the floor: admins only. A member who
    could grant themselves permissions would make this setting pointless.
    """
    if conversation.edit_group_permission == Conversation.EditGroupPermission.MEMBERS:
        return True
    return is_admin


def allows(user, feature_key, subject, *, roles):
    """Whether the subject's own settings allow this, given the role floor.

    Returns True when no rule applies, so a feature nobody narrowed is decided
    by the floor alone.
    """
    if not isinstance(subject, Conversation):
        return True

    rule = _CONVERSATION_RULES.get(feature_key)
    if rule is None:
        return True

    from chat.models import Participant

    return bool(rule(user, subject, Participant.Role.ADMIN in roles))


def narrowed_keys():
    """Feature keys that have a second layer. For checks and diagnostics."""
    return sorted(_CONVERSATION_RULES)
