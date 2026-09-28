"""Who a user is, what the product can do, and which roles may do it.

Three tables, and the split between them is the whole design:

    UserRole      one row per role a user holds        WHO
    Feature       one row per thing that can be done   WHAT
    RoleFeature   one row per (role, feature) cell     WHO MAY DO WHAT

`Feature` is a catalogue seeded from `rbac/registry.py` and never written
through a UI: every row must correspond to a real check in the code, or the
permissions screen shows a promise nothing keeps. `RoleFeature` is the
configuration between the two, and it is the table the matrix screen renders.

TWO AXES, NEVER ONE
-------------------
`UserRole.role` is an ACCOUNT role - standard, commercial, admin - and says
which areas of the product a person may reach at all.

It is not `chat.Participant.Role`, which is a GROUP role - admin or member,
scoped to one conversation - and says what they may do inside it. The same
person is `standard` at account level and `admin` of the Loop they created;
both are true at once, and a single role column could not express it.

They are separate enums on separate models, so code is unambiguous. In prose,
logs and support conversations, always say *account admin* or *group admin*,
never bare "admin".

NOTHING HERE IS READ YET
------------------------
Step 1 of `docs/rbac_plan.md` is the schema alone. There is no resolver, no
`can()`, no signal and no wiring into `chat/`. These tables are additive: the
running app behaves identically with them present and empty.
"""
from django.conf import settings
from django.db import models


class UserRole(models.Model):
    """One role held by one user. Additive - a user holds as many as granted.

    `standard` is stored rather than implied by the absence of a row. It costs
    a row per user and buys three things: the permissions matrix gets a real
    column backed by real data instead of a phantom one, "every standard user"
    is a query rather than a NOT EXISTS, and *absence* stops being meaningful -
    a user with no rows at all is a bug the system can detect rather than a
    state it has to interpret.

    It also keeps one product question out of the schema. An ads-only
    commercial user is simply an account with a `commercial` row and no
    `standard` row; whether such an account is ever created is a business
    decision, and the model does not force it either way.

    `granted_by` is nullable because the rows that arrive by migration or
    signal have no human behind them. Nulls here mean "the system did it",
    which is a different fact from "nobody knows", and the audit log added in
    step 5 is what records the rest.
    """

    class Role(models.TextChoices):
        # Normal app use - chat, groups, calls. Granted at signup.
        STANDARD = 'standard', 'Standard'
        # Plus the commercial area: companies that buy ads. Was `advertiser`
        # until rbac/0008. Not the Business plan, which is a billing tier.
        COMMERCIAL = 'commercial', 'Commercial'
        # Plus every feature in the product. Loopd'n's own team.
        #
        # NOT `is_staff`/`is_superuser`, which stay reserved for the Django
        # admin and carry no product meaning - see docs/rbac_plan.md. A person
        # may hold both; neither implies the other.
        ADMIN = 'admin', 'Account admin'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='account_roles',
    )
    role = models.CharField(max_length=16, choices=Role.choices)

    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['user_id', 'role']
        constraints = [
            # Holding a role twice would double it in every set built from
            # these rows, and would make revoking it a loop rather than a
            # delete.
            models.UniqueConstraint(
                fields=['user', 'role'], name='rbac_userrole_unique_user_role'
            ),
        ]
        indexes = [
            # The resolver's only lookup: every role this user holds.
            models.Index(fields=['user'], name='rbac_userrole_user_idx'),
        ]

    def __str__(self):
        return f'{self.user} is {self.get_role_display()}'


class Feature(models.Model):
    """One thing a user can do, and how the matrix should present it.

    Seeded from `rbac/registry.py` by a data migration in step 2, never created
    through a UI. A feature invented on a screen would grant nothing, because
    no code checks it - so the catalogue is code, and only the grants are data.

    `key` is the string application code passes to `can()`, dotted and
    namespaced by area: `group.message.pin`, `ads.campaign.publish`. It is
    unique across every axis, so one key can never mean two things.
    """

    class Axis(models.TextChoices):
        # Which areas of the product the person may reach. Drives menus.
        ACCOUNT = 'account', 'Account'
        # What they may do inside one conversation.
        CONVERSATION = 'conversation', 'Conversation'
        # What they may do to one company's campaigns (a commercial.Company).
        # Was `advertiser` until rbac/0009.
        COMPANY = 'company', 'Company'

    key = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=128)
    description = models.CharField(max_length=255, blank=True, default='', db_default='')

    # Groups the rows on the permissions screen: 'Messaging', 'Advertising',
    # 'Platform'. Free text rather than choices, because a new area is a
    # registry edit and not a schema change.
    area = models.CharField(max_length=40)

    axis = models.CharField(max_length=16, choices=Axis.choices)

    # What the seeder writes into this feature's RoleFeature rows when it
    # creates them. True is the cheap path - most of what the product does
    # should be available to everyone, and the exceptions get switched off.
    #
    # False for the handful where allow-by-default would fail OPEN: anything
    # under `platform.*`, campaign review, role granting, billing. A feature
    # added later with this left True is granted to everybody the moment its
    # migration runs, which is exactly the accident this flag exists to make
    # deliberate.
    default_allowed = models.BooleanField(default=True, db_default=True)

    # The stronger claim: never grantable below `admin`, whatever the matrix
    # says. Step 8 makes the matrix editable, and without this the editing
    # screen IS the privilege-escalation path.
    is_lockable = models.BooleanField(default=False, db_default=False)

    sort_order = models.PositiveSmallIntegerField(default=0, db_default=0)

    class Meta:
        ordering = ['area', 'sort_order', 'key']
        indexes = [
            models.Index(fields=['axis'], name='rbac_feature_axis_idx'),
        ]

    def __str__(self):
        return self.key


class RoleFeature(models.Model):
    """What one role may use. One row per cell of the permissions matrix.

    `role` is a plain CharField rather than a FK: roles live in three different
    places depending on the axis - `UserRole.Role` for account,
    `chat.Participant.Role` for conversation, and a company's own
    roles later - and a FK could only point at one of them. `Feature.axis` says
    which vocabulary this row's `role` is drawn from.
    """

    role = models.CharField(max_length=16)
    feature = models.ForeignKey(
        Feature, on_delete=models.CASCADE, related_name='role_features'
    )

    # Field default is False on purpose - fail closed. What gets written at
    # seed time is `Feature.default_allowed`, set explicitly by the seeder, so
    # a row created without an explicit value grants nothing rather than
    # everything.
    allowed = models.BooleanField(default=False)

    class Meta:
        ordering = ['feature_id', 'role']
        constraints = [
            # One answer per cell. Two rows for the same pair would make
            # "is this allowed" depend on which the query happened to read.
            models.UniqueConstraint(
                fields=['role', 'feature'],
                name='rbac_rolefeature_unique_role_feature',
            ),
        ]
        indexes = [
            # The resolver reads every cell for a set of roles at once.
            models.Index(fields=['role'], name='rbac_rolefeature_role_idx'),
        ]

    def __str__(self):
        verb = 'may' if self.allowed else 'may not'
        return f'{self.role} {verb} {self.feature_id and self.feature.key}'


class RoleAuditLog(models.Model):
    """Who changed whose access, to what, and when. Append-only.

    §13 requires audit logging, and role changes are the events most likely to
    be asked about after the fact - "who made them an admin?" is a question
    that arrives weeks later, from somebody who cannot read the database.

    THE LABELS ARE NOT REDUNDANT
    ----------------------------
    `actor` and `subject` are SET_NULL, because deleting a user must not delete
    the record of what they were given. But a row reading "granted admin to
    <nobody>" answers nothing, so the names are copied in as text at write
    time. The FK is for joining while the account exists; the label is what
    survives it.

    Covers step 8's matrix edits as well as role grants - `feature` is null for
    a role change and `role` is blank for a matrix change - so that the
    editable permissions screen has somewhere to write from its first day
    rather than growing a second log.
    """

    class Action(models.TextChoices):
        GRANT = 'grant', 'Role granted'
        REVOKE = 'revoke', 'Role revoked'
        # Step 8. Named for the outcome rather than the click, so a row reads
        # the same whether it came from the matrix screen or a migration.
        FEATURE_ALLOW = 'feature.allow', 'Feature allowed for a role'
        FEATURE_DENY = 'feature.deny', 'Feature denied for a role'

    action = models.CharField(max_length=16, choices=Action.choices)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    #: Blank when the system did it - a migration, a signal, a management
    #: command. A different fact from "nobody knows".
    actor_label = models.CharField(max_length=150, blank=True, default='', db_default='')

    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    subject_label = models.CharField(max_length=150, blank=True, default='', db_default='')

    #: The account role granted or revoked. Blank for a matrix change.
    role = models.CharField(max_length=16, blank=True, default='', db_default='')

    #: The feature whose grant changed. Null for a role change.
    feature = models.ForeignKey(
        Feature,
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    feature_key = models.CharField(max_length=64, blank=True, default='', db_default='')

    note = models.CharField(max_length=255, blank=True, default='', db_default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['-created_at'], name='rbac_audit_recent_idx'),
            models.Index(fields=['subject'], name='rbac_audit_subject_idx'),
        ]

    def __str__(self):
        who = self.actor_label or 'the system'
        target = self.subject_label or self.feature_key or '?'
        return f'{who} {self.get_action_display().lower()}: {self.role or self.feature_key} for {target}'

    @classmethod
    def record(cls, action, *, actor=None, subject=None, role='', feature=None, note=''):
        """Write one row, copying the labels off whatever was passed.

        The single write path, so a caller cannot record a grant and forget the
        label that makes it readable a year later.
        """
        return cls.objects.create(
            action=action,
            actor=actor,
            actor_label=getattr(actor, 'username', '') or '',
            subject=subject,
            subject_label=getattr(subject, 'username', '') or '',
            role=role,
            feature=feature,
            feature_key=getattr(feature, 'key', '') or '',
            note=note,
        )
