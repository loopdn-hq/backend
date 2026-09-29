"""The feature catalogue, and what each role may do with it.

This module is the seed source. A data migration copies it into `Feature` and
`RoleFeature`; from then on the DATABASE is authoritative and this file is
history. New features arrive by migration, never by editing here and hoping.

WHAT EARNS A ROW
----------------
    A feature earns a row only if two roles could reasonably differ on it.
    If every role gets the same answer, it is not a feature - it is behaviour.

That rule is what keeps this file at ~20 entries instead of ~120. Leaving a
group, marking a conversation read, reacting to a message and searching are all
things every member does; encoding them would add rows that read identically in
every column and make the permissions screen unreadable.

THE FLOOR, NOT THE WHOLE ANSWER
-------------------------------
A grant here is the FLOOR: what the role permits in principle. Some features
have a second layer - the conversation's own settings - which can only narrow
it, and that layer lands with `rbac/policy.py` in step 3:

    group.member.add     floor allows a member; `add_member_permission` decides
    group.settings.edit  floor allows a member; `edit_group_permission` decides
    group.message.send   floor allows a member; broadcast mode decides

So a `True` here does not mean "members can always do this". It means "the role
is not what stops them". Read the two together, which is what `can()` does.

WHAT IS DELIBERATELY ABSENT
---------------------------
No campaign or company-scope features yet. The `commercial` app's API so far
is companies (slice 0 of `docs/campaigns_final_plan.md`), and a row for
`ads.campaign.publish` before campaigns exist would be a promise with no check
behind it - exactly what the catalogue-in-code rule exists to prevent. They
arrive with the campaign API (step 9 and `docs/campaigns_final_plan.md`).

`ads.access` is the one exception. It gates the whole commercial API
(`commercial.permissions.CanUseCommercial`) and the menu item step 6 adds.
When the campaign features do arrive, `commercial` is the role that gets them -
confirmed, not assumed.
"""

#: `Feature.axis` values, repeated here so the registry reads without the model.
ACCOUNT = 'account'
CONVERSATION = 'conversation'

#: Account roles, from `rbac.models.UserRole.Role`.
ADMIN = 'admin'
COMMERCIAL = 'commercial'
STANDARD = 'standard'

#: Group roles, from `chat.models.Participant.Role`. The conversation axis
#: draws its role names from here, which is why `RoleFeature.role` is a plain
#: CharField and not a FK - see `rbac/models.py`.
GROUP_ADMIN = 'admin'
GROUP_MEMBER = 'member'


# ---------------------------------------------------------------------------
# ACCOUNT AXIS - which areas of the product a person may reach. Drives menus.
#
# `commercial` is purely ADDITIVE here: it grants the advertising area and
# nothing else. A company that also uses Loopd'n normally holds `standard` as
# well and gets chat through that, while an ads-only account holds `commercial`
# alone and reaches only the ads area. That makes the ads-only question a
# per-account grant rather than a schema decision - see open question 1.
# ---------------------------------------------------------------------------

ACCOUNT_FEATURES = [
    dict(
        key='chat.access', label='Open chat', area='Messaging',
        description='Reach the Chats area and take part in conversations.',
        sort_order=10,
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: True},
    ),
    dict(
        key='chat.group.create', label='Create a group', area='Messaging',
        description='Start a new Loop. Also bounded by the plan limit in '
                    'billing/limits.py, which is a separate question.',
        sort_order=20,
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: True},
    ),
    dict(
        key='chat.direct.start', label='Start a direct message', area='Messaging',
        description='Open a one-to-one conversation.',
        sort_order=30,
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: True},
    ),
    dict(
        key='broadcast.access', label='Open the broadcast area', area='Messaging',
        description='Reach the Broadcast section, where admin-only '
                    'announcement Loops are listed.',
        sort_order=32,
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: True},
    ),
    dict(
        key='activity.access', label='Open the activity feed', area='Messaging',
        description='Reach Activity - join requests, role changes, delivery '
                    'failures. The notification app already decides WHAT lands '
                    'there; this is whether the area is reachable at all.',
        sort_order=34,
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: True},
    ),
    dict(
        key='find_groups.access', label='Discover and join public groups',
        area='Messaging', sort_order=36,
        description='Reach Find groups. Joining an individual Loop is still '
                    'governed by that Loop\'s own join_mode.',
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: True},
    ),
    dict(
        key='analytics.access', label='Open group analytics', area='Messaging',
        description='Reach Analytics. Narrowed FURTHER by the plan - the role '
                    'decides whether the area exists for this person, the plan '
                    'decides whether they have paid for it. Two different '
                    'questions; billing is out of scope here.',
        sort_order=38,
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: True},
    ),
    dict(
        key='billing.manage', label='Manage own billing', area='Billing',
        description='View and change this account\'s own plan, credits and '
                    'payment method. Never anybody else\'s.',
        sort_order=40,
        grants={ADMIN: True, COMMERCIAL: True, STANDARD: True},
    ),
    dict(
        key='ads.access', label='Open the advertising area', area='Advertising',
        description='Reach the Advertise section. The campaign features '
                    'themselves arrive with the ads app.',
        sort_order=50,
        grants={ADMIN: True, COMMERCIAL: True, STANDARD: False},
    ),

    # Platform tools. default_allowed=False on every one: these are where
    # allow-by-default would fail OPEN, and a feature added later that forgets
    # to opt out is granted to everybody the moment its migration runs.
    dict(
        key='platform.permissions.view', label='View the permissions matrix',
        area='Platform', sort_order=60, default_allowed=False,
        description='Read which role may use which feature. Step 7.',
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: False},
    ),
    dict(
        key='platform.permissions.edit', label='Change the permissions matrix',
        area='Platform', sort_order=65, default_allowed=False, is_lockable=True,
        description='Change which role may use which feature. Lockable and '
                    'separate from viewing, so a support role can read the '
                    'matrix without being able to rewrite it.',
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: False},
    ),
    dict(
        key='platform.role.grant', label='Grant and revoke account roles',
        area='Platform', sort_order=70, default_allowed=False, is_lockable=True,
        description='Add or remove a UserRole. Lockable: never grantable below '
                    'admin, or the matrix screen becomes the escalation path.',
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: False},
    ),
    dict(
        key='platform.settings', label='Change platform settings',
        area='Platform', sort_order=80, default_allowed=False, is_lockable=True,
        description='Global switches such as the SMS and ads kill switches.',
        grants={ADMIN: True, COMMERCIAL: False, STANDARD: False},
    ),
]


# ---------------------------------------------------------------------------
# CONVERSATION AXIS - what a person may do inside one group.
#
# TRANSCRIBED, not decided. Every grant below is what the code does today:
#
#   group.message.send        allows_posting_by          chat/models.py
#   group.message.pin         may_pin                    chat/models.py
#   group.message.delete.any  _delete_message            viewsets/v1/...
#   group.member.add          may_add_members            chat/models.py
#   group.member.remove       membership.remove          _require_admin
#   group.member.role.set     membership.set_role        _require_admin
#   group.member.rename       rename action              is_admin
#   group.join_request.review requests + approve/decline is_admin
#   group.join_code.manage    join_code action           is_admin
#   group.invite.phone        invite action              is_admin
#   group.settings.edit       may_edit_group             chat/models.py
#   group.settings.policy     ADMIN_ONLY_SETTINGS        chat/serializers.py
#   group.number.provision    provision_number action    viewsets/v1/...
#
# Step 3's exit criteria is that `can()` reproduces those answers exactly, so
# this table is checked against behaviour rather than trusted.
# ---------------------------------------------------------------------------

CONVERSATION_FEATURES = [
    dict(
        key='group.message.send', label='Post a message', area='Groups',
        description='Send into this conversation. Broadcast mode narrows this '
                    'to admins - see allows_posting_by.',
        sort_order=10,
        grants={GROUP_ADMIN: True, GROUP_MEMBER: True},
    ),
    dict(
        key='group.message.pin', label='Pin a message', area='Groups',
        description='Pin or unpin the one message shown above the thread. '
                    'A one-to-one has no admins, so either person may pin '
                    'there - handled in policy, not here.',
        sort_order=20, default_allowed=False,
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.message.delete.any', label="Delete anyone's message",
        area='Groups', sort_order=30, default_allowed=False,
        description='The moderation half of delete. Everyone may already '
                    'delete their own, which is not a role question.',
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.member.add', label='Add members', area='Groups',
        description='Narrowed further by add_member_permission, which an '
                    'admin can loosen to members.',
        sort_order=40,
        grants={GROUP_ADMIN: True, GROUP_MEMBER: True},
    ),
    dict(
        key='group.member.remove', label='Remove or block a member',
        area='Groups', sort_order=50, default_allowed=False,
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.member.role.set', label='Change a member\'s role',
        area='Groups', sort_order=60, default_allowed=False,
        description='Promote or demote. The escalation guard: a role that can '
                    'grant roles can grant itself anything.',
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.member.rename', label='Name a member with no account',
        area='Groups', sort_order=70, default_allowed=False,
        description='Somebody who joined by texting JOIN never gave a name.',
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.join_request.review', label='Review join requests',
        area='Groups', sort_order=80, default_allowed=False,
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.join_code.manage', label='See and regenerate the join code',
        area='Groups', sort_order=90, default_allowed=False,
        description='The code lets a stranger in, so it is kept off the '
                    'conversation serializer entirely.',
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.invite.phone', label='Invite by phone number',
        area='Groups', sort_order=100, default_allowed=False,
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.settings.edit', label='Edit name, photo and description',
        area='Groups', sort_order=110,
        description='Narrowed further by edit_group_permission. Never covers '
                    'the policy settings below.',
        grants={GROUP_ADMIN: True, GROUP_MEMBER: True},
    ),
    dict(
        key='group.settings.policy', label='Change group policy settings',
        area='Groups', sort_order=120, default_allowed=False,
        description='mode, join_mode, add_member_permission, '
                    'edit_group_permission, sms_delivery_enabled, '
                    'sms_replies_enabled, voice_messages_enabled. A member who '
                    'could change these would make the others meaningless.',
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
    dict(
        key='group.number.provision', label="Get the group's phone number",
        area='Groups', sort_order=130, default_allowed=False,
        description='For a group whose number was never bought. Spends money '
                    'every month, which is why it is not folded into the '
                    'policy settings: an operator may want admins changing '
                    'those without buying numbers.',
        grants={GROUP_ADMIN: True, GROUP_MEMBER: False},
    ),
]


def all_features():
    """Every feature with its axis attached, in seeding order."""
    for entry in ACCOUNT_FEATURES:
        yield {**entry, 'axis': ACCOUNT}
    for entry in CONVERSATION_FEATURES:
        yield {**entry, 'axis': CONVERSATION}
