"""Schema constraints, and what the seed migrations put in the tables.

There is no resolver yet, so there is nothing to test about behaviour. What
these cover is the schema's own promises - a role cannot be held twice, a cell
cannot have two answers, a RoleFeature created without an explicit value grants
nothing - and then that the seeded data says what `rbac/registry.py` says.

The constraint tests use made-up `test.*` feature keys rather than real ones.
The seed migrations have already run against the test database, so a fixture
using `chat.access` would collide with the row that is genuinely there, and the
test would be asserting against the seed rather than the constraint.
"""
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from rest_framework.exceptions import PermissionDenied

from rest_framework import status
from rest_framework.test import APIClient

from user.tests import EnvelopeAssertions

from chat.models import Conversation, Participant
from rbac import services
from rbac.models import Feature, RoleAuditLog, RoleFeature, UserRole
from rbac.resolver import can, features_for, require


class UserRoleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw')

    def test_a_user_holds_several_roles_at_once(self):
        # `standard` is already there - the signup signal grants it.
        UserRole.objects.create(user=self.user, role=UserRole.Role.COMMERCIAL)

        self.assertEqual(
            set(self.user.account_roles.values_list('role', flat=True)),
            {'standard', 'commercial'},
        )

    def test_the_same_role_cannot_be_held_twice(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserRole.objects.create(user=self.user, role=UserRole.Role.STANDARD)

    def test_granted_by_survives_the_granter_being_deleted(self):
        granter = User.objects.create_user(username='admin', password='pw')
        role = UserRole.objects.create(
            user=self.user, role=UserRole.Role.COMMERCIAL, granted_by=granter
        )

        granter.delete()
        role.refresh_from_db()

        # SET_NULL, not CASCADE: deleting the person who granted a role must
        # not silently revoke it for everybody they ever granted it to.
        self.assertIsNone(role.granted_by)
        self.assertTrue(UserRole.objects.filter(pk=role.pk).exists())

    def test_roles_go_when_the_user_does(self):
        UserRole.objects.create(user=self.user, role=UserRole.Role.COMMERCIAL)
        user_id = self.user.pk

        self.user.delete()

        self.assertEqual(UserRole.objects.filter(user_id=user_id).count(), 0)

    def test_str_names_the_person_and_the_role(self):
        role = UserRole.objects.create(user=self.user, role=UserRole.Role.ADMIN)

        self.assertIn('alice', str(role))
        self.assertIn('Account admin', str(role))


class FeatureTests(TestCase):
    def test_a_key_cannot_mean_two_things(self):
        Feature.objects.create(
            key='test.thing.do', label='Do the thing',
            area='Testing', axis=Feature.Axis.CONVERSATION,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Feature.objects.create(
                key='test.thing.do', label='Something else',
                area='Testing', axis=Feature.Axis.ACCOUNT,
            )

    def test_defaults_are_allow_and_not_lockable(self):
        feature = Feature.objects.create(
            key='test.thing.open', label='Open the thing',
            area='Testing', axis=Feature.Axis.ACCOUNT,
        )

        # Allow-by-default is the seeding posture: most of what the product
        # does should reach everyone, and the exceptions opt out.
        self.assertTrue(feature.default_allowed)
        self.assertFalse(feature.is_lockable)

    def test_str_is_the_key(self):
        feature = Feature.objects.create(
            key='test.thing.publish', label='Publish the thing',
            area='Testing', axis=Feature.Axis.ACCOUNT,
        )

        self.assertEqual(str(feature), 'test.thing.publish')


class RoleFeatureTests(TestCase):
    def setUp(self):
        self.feature = Feature.objects.create(
            key='test.thing.create', label='Create the thing',
            area='Testing', axis=Feature.Axis.ACCOUNT,
        )

    def test_a_cell_created_without_a_value_grants_nothing(self):
        cell = RoleFeature.objects.create(
            role=UserRole.Role.STANDARD, feature=self.feature
        )

        # Fail closed. The seeder writes Feature.default_allowed explicitly;
        # anything that forgets to must not hand out a capability.
        self.assertFalse(cell.allowed)

    def test_one_answer_per_cell(self):
        RoleFeature.objects.create(
            role=UserRole.Role.COMMERCIAL, feature=self.feature, allowed=True
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            RoleFeature.objects.create(
                role=UserRole.Role.COMMERCIAL, feature=self.feature, allowed=False
            )

    def test_the_same_role_may_answer_for_different_features(self):
        other = Feature.objects.create(
            key='test.thing.review', label='Review the thing',
            area='Testing', axis=Feature.Axis.ACCOUNT,
            default_allowed=False, is_lockable=True,
        )

        RoleFeature.objects.create(
            role=UserRole.Role.COMMERCIAL, feature=self.feature, allowed=True
        )
        RoleFeature.objects.create(
            role=UserRole.Role.COMMERCIAL, feature=other, allowed=False
        )

        self.assertEqual(
            RoleFeature.objects.filter(
                role='commercial', feature__area='Testing'
            ).count(),
            2,
        )

    def test_cells_go_when_the_feature_does(self):
        RoleFeature.objects.create(
            role=UserRole.Role.STANDARD, feature=self.feature, allowed=True
        )
        feature_id = self.feature.pk

        self.feature.delete()

        self.assertEqual(
            RoleFeature.objects.filter(feature_id=feature_id).count(), 0
        )

    def test_str_says_whether_the_role_may(self):
        allowed = RoleFeature.objects.create(
            role=UserRole.Role.COMMERCIAL, feature=self.feature, allowed=True
        )
        self.assertIn('may test.thing.create', str(allowed))

        denied = RoleFeature.objects.create(
            role=UserRole.Role.STANDARD, feature=self.feature, allowed=False
        )
        self.assertIn('may not', str(denied))


class RoleFeatureAcceptsEveryAxisTests(TestCase):
    """`role` is a plain CharField because roles come from three vocabularies.

    Account roles are `UserRole.Role`, conversation roles are
    `chat.Participant.Role`, and the advertiser org has its own. A FK could
    only point at one of them, so `Feature.axis` is what says which vocabulary
    a row's `role` is drawn from.
    """

    def test_a_conversation_axis_cell_stores_a_group_role(self):
        from chat.models import Participant

        feature = Feature.objects.create(
            key='test.group.thing', label='Do the group thing',
            area='Testing', axis=Feature.Axis.CONVERSATION,
            default_allowed=False,
        )
        cell = RoleFeature.objects.create(
            role=Participant.Role.ADMIN, feature=feature, allowed=True
        )

        self.assertEqual(cell.role, 'admin')
        self.assertEqual(cell.feature.axis, 'conversation')


class SeedTests(TestCase):
    """Step 2: what the data migrations put in the tables.

    The migrations have already run against the test database, so these read
    the seeded rows rather than re-running anything.
    """

    def test_every_registry_feature_is_seeded(self):
        from rbac.registry import all_features

        expected = {entry['key'] for entry in all_features()}

        self.assertEqual(set(Feature.objects.values_list('key', flat=True)), expected)

    def test_every_feature_has_a_cell_for_every_role_on_its_axis(self):
        from rbac.registry import all_features

        for entry in all_features():
            feature = Feature.objects.get(key=entry['key'])
            self.assertEqual(
                set(feature.role_features.values_list('role', flat=True)),
                set(entry['grants']),
                f'{entry["key"]} is missing a cell',
            )

    def test_grants_match_the_registry_exactly(self):
        from rbac.registry import all_features

        for entry in all_features():
            for role, allowed in entry['grants'].items():
                cell = RoleFeature.objects.get(feature__key=entry['key'], role=role)
                self.assertEqual(
                    cell.allowed, allowed, f'{entry["key"]} / {role}'
                )

    def test_no_platform_feature_is_allowed_below_admin(self):
        """The fail-open guard, asserted against the data and not the intent."""
        leaked = RoleFeature.objects.filter(
            feature__key__startswith='platform.', allowed=True
        ).exclude(role=UserRole.Role.ADMIN)

        self.assertFalse(
            leaked.exists(),
            f'platform features granted below admin: {list(leaked)}',
        )

    def test_lockable_features_are_admin_only(self):
        leaked = RoleFeature.objects.filter(
            feature__is_lockable=True, allowed=True
        ).exclude(role=UserRole.Role.ADMIN)

        self.assertFalse(leaked.exists())

    def test_the_conversation_seed_reproduces_current_behaviour(self):
        """A group member still cannot do the things they cannot do today.

        Transcribed from `_require_admin`, `is_admin` and ADMIN_ONLY_SETTINGS.
        If one of these flips, the matrix has drifted from the code and step 4
        would ship the drift.
        """
        admin_only = [
            'group.message.pin',
            'group.message.delete.any',
            'group.member.remove',
            'group.member.role.set',
            'group.member.rename',
            'group.join_request.review',
            'group.join_code.manage',
            'group.invite.phone',
            'group.settings.policy',
        ]
        for key in admin_only:
            self.assertFalse(
                RoleFeature.objects.get(feature__key=key, role='member').allowed,
                f'{key} must stay admin-only',
            )
            self.assertTrue(
                RoleFeature.objects.get(feature__key=key, role='admin').allowed,
                f'{key} must be allowed for a group admin',
            )

    def test_the_settings_gated_features_leave_the_floor_open(self):
        """These three are narrowed by the conversation's own settings.

        The role must NOT be what stops a member, or `add_member_permission`
        and `edit_group_permission` could never loosen anything and broadcast
        mode would be the only posting rule.
        """
        for key in ['group.member.add', 'group.settings.edit', 'group.message.send']:
            self.assertTrue(
                RoleFeature.objects.get(feature__key=key, role='member').allowed,
                f'{key} floor must permit a member; policy narrows it',
            )

    def test_commercial_is_additive_and_carries_no_messaging(self):
        """`commercial` grants the ads area and nothing else.

        A company that also uses Loopd'n normally holds `standard` too and
        gets chat through that. An ads-only account holds `commercial` alone
        and reaches only the ads area - which is what makes that a per-account
        grant rather than a schema decision.
        """
        self.assertTrue(
            RoleFeature.objects.get(feature__key='ads.access', role='commercial').allowed
        )
        for key in ['chat.access', 'chat.group.create', 'chat.direct.start']:
            self.assertFalse(
                RoleFeature.objects.get(feature__key=key, role='commercial').allowed
            )
            self.assertTrue(
                RoleFeature.objects.get(feature__key=key, role='standard').allowed
            )

    def test_standard_cannot_reach_the_ads_area(self):
        self.assertFalse(
            RoleFeature.objects.get(feature__key='ads.access', role='standard').allowed
        )


class StandardRoleOnSignupTests(TestCase):
    def test_a_new_user_gets_standard(self):
        user = User.objects.create_user(username='bob', password='pw')

        self.assertEqual(
            list(user.account_roles.values_list('role', flat=True)), ['standard']
        )

    def test_a_superuser_gets_standard_but_not_admin(self):
        """The `admin` role is a deliberate grant, not a side effect.

        rbac/0003 bootstrapped the existing superusers once. Syncing the two on
        every save would rebuild the coupling rbac exists to remove - and would
        mean creating a Django-admin login silently handed out every product
        capability.
        """
        user = User.objects.create_superuser(
            username='root', email='root@example.com', password='pw'
        )

        self.assertEqual(
            list(user.account_roles.values_list('role', flat=True)), ['standard']
        )

    def test_saving_an_existing_user_does_not_duplicate_the_role(self):
        user = User.objects.create_user(username='carol', password='pw')
        user.first_name = 'Carol'
        user.save()

        self.assertEqual(user.account_roles.filter(role='standard').count(), 1)

    def test_a_user_who_already_has_the_role_is_left_alone(self):
        """get_or_create, so a fixture or a retry cannot break user creation."""
        user = User.objects.create_user(username='dave', password='pw')
        original = user.account_roles.get(role='standard')

        User.objects.get(pk=user.pk).save()

        self.assertEqual(user.account_roles.get(role='standard').pk, original.pk)


class AccountAxisTests(TestCase):
    """`can()` on the account axis - no subject."""

    def setUp(self):
        self.user = User.objects.create_user(username='erin', password='pw')

    def test_standard_reaches_chat_but_not_ads(self):
        self.assertTrue(can(self.user, 'chat.access'))
        self.assertTrue(can(self.user, 'chat.group.create'))
        self.assertFalse(can(self.user, 'ads.access'))

    def test_commercial_reaches_ads_and_keeps_chat_through_standard(self):
        UserRole.objects.create(user=self.user, role=UserRole.Role.COMMERCIAL)

        self.assertTrue(can(self.user, 'ads.access'))
        # Chat comes from the `standard` row, not from `commercial`.
        self.assertTrue(can(self.user, 'chat.access'))

    def test_an_ads_only_account_reaches_ads_and_nothing_else(self):
        """`commercial` alone, without `standard` - the per-account choice."""
        self.user.account_roles.filter(role=UserRole.Role.STANDARD).delete()
        UserRole.objects.create(user=self.user, role=UserRole.Role.COMMERCIAL)

        self.assertTrue(can(self.user, 'ads.access'))
        self.assertFalse(can(self.user, 'chat.access'))
        self.assertFalse(can(self.user, 'chat.group.create'))

    def test_platform_features_need_the_admin_role(self):
        self.assertFalse(can(self.user, 'platform.role.grant'))

        UserRole.objects.create(user=self.user, role=UserRole.Role.ADMIN)

        self.assertTrue(can(self.user, 'platform.role.grant'))

    def test_a_superuser_without_the_admin_role_gets_nothing_extra(self):
        """is_superuser is Django admin access and carries no product meaning."""
        root = User.objects.create_superuser(
            username='root2', email='r@example.com', password='pw'
        )

        self.assertFalse(can(root, 'platform.role.grant'))
        self.assertFalse(can(root, 'platform.settings'))

    def test_a_user_with_no_roles_is_denied_everything(self):
        self.user.account_roles.all().delete()

        self.assertFalse(can(self.user, 'chat.access'))
        self.assertEqual(features_for(self.user), set())

    def test_an_unknown_key_fails_closed(self):
        self.assertFalse(can(self.user, 'nonsense.not.a.feature'))

    def test_anonymous_is_denied(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(can(AnonymousUser(), 'chat.access'))

    def test_features_for_returns_the_account_set(self):
        self.assertEqual(
            features_for(self.user),
            {'chat.access', 'chat.group.create', 'chat.direct.start',
             'broadcast.access', 'activity.access', 'find_groups.access',
             'analytics.access', 'billing.manage'},
        )

    def test_the_new_area_features_follow_the_same_rule_as_chat(self):
        """Areas are `standard`'s, not `commercial`'s.

        A commercial user who also uses Loopd'n normally holds `standard` too and
        reaches them through that; an ads-only account does not.
        """
        for key in ['broadcast.access', 'activity.access',
                    'find_groups.access', 'analytics.access']:
            self.assertTrue(can(self.user, key), key)

        self.user.account_roles.filter(role=UserRole.Role.STANDARD).delete()
        UserRole.objects.create(user=self.user, role=UserRole.Role.COMMERCIAL)

        for key in ['broadcast.access', 'activity.access',
                    'find_groups.access', 'analytics.access']:
            self.assertFalse(can(self.user, key), key)


class ConversationParityTests(TestCase):
    """`can()` must answer exactly what the code answers today.

    This is step 3's whole exit criteria. Each test asserts BOTH the expected
    value and that `Conversation`'s existing method agrees - so if step 4's
    rewire ever drifts, these fail rather than the drift shipping.
    """

    def setUp(self):
        self.admin = User.objects.create_user(username='amy', password='pw')
        self.member = User.objects.create_user(username='mo', password='pw')
        self.group = Conversation.start_group(self.admin, 'Family')
        self.group.add_participant(self.member)

    def _participant(self, user):
        return Participant.objects.get(conversation=self.group, user=user)

    def test_admin_only_features_match_is_admin(self):
        for key in ['group.member.remove', 'group.member.role.set',
                    'group.member.rename', 'group.join_request.review',
                    'group.join_code.manage', 'group.invite.phone',
                    'group.settings.policy', 'group.message.delete.any']:
            self.assertTrue(can(self.admin, key, self.group), key)
            self.assertFalse(can(self.member, key, self.group), key)

        self.assertTrue(self.group.is_admin(self.admin))
        self.assertFalse(self.group.is_admin(self.member))

    def test_pin_matches_may_pin_in_a_group(self):
        self.assertTrue(can(self.admin, 'group.message.pin', self.group))
        self.assertFalse(can(self.member, 'group.message.pin', self.group))

        self.assertIs(self.group.may_pin(self.admin), True)
        self.assertIs(self.group.may_pin(self.member), False)

    def test_pinning_a_direct_conversation_is_membership_not_a_role(self):
        """A DM has no admins, so pinning there is not an RBAC question.

        `can()` says no for both sides - correctly, because neither holds a
        group role that grants it. `may_pin` says yes for both, through its
        membership branch, which runs before any role is consulted.

        Step 4 must keep that branch OUTSIDE the `can()` call. Folding it in
        would mean either letting policy widen a denial, or seeding
        `group.message.pin` open for `member` - which would make the
        permissions matrix claim members can pin, false in every group.
        """
        direct, _ = Conversation.start_direct(self.admin, self.member)

        self.assertFalse(can(self.admin, 'group.message.pin', direct))
        self.assertFalse(can(self.member, 'group.message.pin', direct))

        self.assertIs(direct.may_pin(self.admin), True)
        self.assertIs(direct.may_pin(self.member), True)

    def test_add_members_matches_may_add_members_both_ways(self):
        self.assertTrue(can(self.admin, 'group.member.add', self.group))
        self.assertFalse(can(self.member, 'group.member.add', self.group))
        self.assertIs(self.group.may_add_members(self.member), False)

        self.group.add_member_permission = Conversation.MemberAddPermission.MEMBERS
        self.group.save(update_fields=['add_member_permission'])
        self.group.refresh_from_db()

        self.assertTrue(can(self.member, 'group.member.add', self.group))
        self.assertIs(self.group.may_add_members(self.member), True)

    def test_edit_group_matches_may_edit_group_both_ways(self):
        self.assertFalse(can(self.member, 'group.settings.edit', self.group))
        self.assertIs(self.group.may_edit_group(self.member), False)

        self.group.edit_group_permission = Conversation.EditGroupPermission.MEMBERS
        self.group.save(update_fields=['edit_group_permission'])
        self.group.refresh_from_db()

        self.assertTrue(can(self.member, 'group.settings.edit', self.group))
        self.assertIs(self.group.may_edit_group(self.member), True)

    def test_loosening_edit_never_loosens_the_policy_settings(self):
        """ADMIN_ONLY_SETTINGS stays admin-only however the group is configured.

        A member who could change `add_member_permission` would make every
        other setting on this list meaningless.
        """
        self.group.edit_group_permission = Conversation.EditGroupPermission.MEMBERS
        self.group.save(update_fields=['edit_group_permission'])
        self.group.refresh_from_db()

        self.assertTrue(can(self.member, 'group.settings.edit', self.group))
        self.assertFalse(can(self.member, 'group.settings.policy', self.group))

    def test_posting_matches_allows_posting_by_in_both_modes(self):
        self.assertTrue(can(self.member, 'group.message.send', self.group))
        self.assertIs(
            self.group.allows_posting_by(self._participant(self.member)), True
        )

        self.group.mode = Conversation.Mode.BROADCAST
        self.group.save(update_fields=['mode'])
        self.group.refresh_from_db()

        self.assertTrue(can(self.admin, 'group.message.send', self.group))
        self.assertFalse(can(self.member, 'group.message.send', self.group))
        self.assertIs(
            self.group.allows_posting_by(self._participant(self.member)), False
        )

    def test_a_non_member_is_denied_everything_in_the_group(self):
        outsider = User.objects.create_user(username='olly', password='pw')

        self.assertFalse(can(outsider, 'group.message.send', self.group))
        self.assertEqual(features_for(outsider, self.group), set())

    def test_a_removed_member_keeps_nothing(self):
        """Status is checked, not just membership: removal must take powers."""
        participant = self._participant(self.member)
        participant.status = Participant.Status.REMOVED
        participant.save(update_fields=['status'])

        self.assertFalse(can(self.member, 'group.message.send', self.group))

    def test_a_pending_member_has_nothing_yet(self):
        participant = self._participant(self.member)
        participant.status = Participant.Status.PENDING
        participant.save(update_fields=['status'])

        self.assertFalse(can(self.member, 'group.message.send', self.group))

    def test_account_roles_do_not_leak_into_a_conversation(self):
        """The two axes are separate. An account admin is not a group admin."""
        UserRole.objects.create(user=self.member, role=UserRole.Role.ADMIN)

        self.assertTrue(can(self.member, 'platform.role.grant'))
        self.assertFalse(can(self.member, 'group.member.role.set', self.group))

    def test_features_for_applies_the_narrowing_rules(self):
        keys = features_for(self.member, self.group)

        self.assertIn('group.message.send', keys)
        self.assertNotIn('group.member.add', keys)
        self.assertNotIn('group.settings.policy', keys)

        self.group.mode = Conversation.Mode.BROADCAST
        self.group.save(update_fields=['mode'])
        self.group.refresh_from_db()

        self.assertNotIn('group.message.send', features_for(self.member, self.group))

    def test_features_for_costs_a_bounded_number_of_queries(self):
        """This runs on every page load - see "Why this shape".

        Cold: the member's role, then the grants for that role. Warm: the role
        only, because the grants lookup depends on (roles, axis) and there are
        a handful of those across the whole product.
        """
        from django.core.cache import cache

        cache.clear()
        with self.assertNumQueries(2):
            features_for(self.member, self.group)

        with self.assertNumQueries(1):
            features_for(self.member, self.group)

    def test_editing_the_matrix_takes_effect_at_once(self):
        """The cache must not outlive the grant it caches.

        Immediate in the process that made the change - a signal clears it. The
        per-process default cache means another worker can serve the old answer
        for up to a minute, which is why a user's ROLES are never cached: taking
        somebody's access away is the operation that has to be instant.
        """
        self.assertTrue(can(self.admin, 'group.message.pin', self.group))

        cell = RoleFeature.objects.get(
            feature__key='group.message.pin', role='admin'
        )
        cell.allowed = False
        cell.save(update_fields=['allowed'])

        self.assertFalse(can(self.admin, 'group.message.pin', self.group))


class RequireTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='fay', password='pw')

    def test_require_is_silent_when_allowed(self):
        self.assertIsNone(require(self.user, 'chat.access'))

    def test_require_raises_with_the_features_own_label(self):
        with self.assertRaises(PermissionDenied) as caught:
            require(self.user, 'ads.access')

        self.assertIn('open the advertising area', str(caught.exception).lower())

    def test_an_unknown_key_still_refuses(self):
        with self.assertRaises(PermissionDenied):
            require(self.user, 'nonsense.not.a.feature')


class AxisTypeTests(TestCase):
    def test_an_unknown_subject_type_is_refused_loudly(self):
        """Fail closed at runtime, loud at the call site.

        A subject nobody wrote a resolver for must not quietly resolve to the
        account axis - that would answer a conversation question with account
        roles.
        """
        user = User.objects.create_user(username='gus', password='pw')

        with self.assertRaises(TypeError):
            can(user, 'chat.access', subject=object())


class StartupCheckTests(TestCase):
    """The guards in rbac/checks.py, proven to fire rather than assumed to.

    A typo in a feature key silently denies - `can()` returns False for
    anything it does not recognise. These are what turn that into a loud
    failure at boot instead of a bug report about a missing button.
    """

    def test_a_clean_database_passes_every_check(self):
        from rbac import checks

        self.assertEqual(checks.registry_matches_database(None), [])
        self.assertEqual(checks.every_feature_has_a_grant_per_role(None), [])
        self.assertEqual(checks.lockable_features_are_admin_only(None), [])

    def test_a_missing_seed_row_is_an_error(self):
        from rbac import checks

        Feature.objects.filter(key='chat.access').delete()

        errors = checks.registry_matches_database(None)

        self.assertEqual([e.id for e in errors], ['rbac.E001'])
        self.assertIn('chat.access', errors[0].msg)

    def test_a_row_the_registry_no_longer_names_is_a_warning(self):
        """A warning, not an error: somebody mid-removal still grants correctly."""
        from rbac import checks

        Feature.objects.create(
            key='test.retired', label='Retired', area='Testing',
            axis=Feature.Axis.ACCOUNT,
        )

        errors = checks.registry_matches_database(None)

        self.assertEqual([e.id for e in errors], ['rbac.W001'])

    def test_a_missing_cell_is_reported(self):
        from rbac import checks

        RoleFeature.objects.filter(
            feature__key='chat.access', role='standard'
        ).delete()

        warnings = checks.every_feature_has_a_grant_per_role(None)

        self.assertEqual([w.id for w in warnings], ['rbac.W002'])

    def test_a_lockable_feature_granted_below_admin_is_an_error(self):
        """The escalation guard. Step 8's editable matrix depends on this."""
        from rbac import checks

        RoleFeature.objects.filter(
            feature__key='platform.role.grant', role='standard'
        ).update(allowed=True)

        errors = checks.lockable_features_are_admin_only(None)

        self.assertEqual([e.id for e in errors], ['rbac.E003'])
        self.assertIn('platform.role.grant', errors[0].msg)


class CatalogueSurvivesTruncationTests(TransactionTestCase):
    """The grants are required configuration, not sample data.

    A TransactionTestCase truncates every table between tests, and so does
    `manage.py flush`. With an empty RoleFeature table `can()` denies
    everything, so a truncation would take the product down rather than just
    losing some rows - a group admin would stop being able to administer their
    own group.

    Django emits post_migrate after a flush, which is how `auth.Permission`
    survives the same treatment. `rbac/apps.py` connects `ensure_catalogue`
    there for the same reason. This test is what proves it, and it has to be a
    TransactionTestCase to do so - a TestCase rolls back and never truncates.
    """

    def test_the_catalogue_is_present_in_a_truncating_test(self):
        self.assertTrue(Feature.objects.exists())
        self.assertTrue(RoleFeature.objects.filter(allowed=True).exists())

    def test_an_admin_can_still_administer_after_truncation(self):
        admin = User.objects.create_user(username='hana', password='pw')
        group = Conversation.start_group(admin, 'Survivors')

        self.assertTrue(can(admin, 'group.member.add', group))
        self.assertTrue(group.may_add_members(admin))

    def test_reseeding_creates_what_is_missing_and_touches_nothing_else(self):
        from rbac.seeding import ensure_catalogue

        cell = RoleFeature.objects.get(
            feature__key='chat.access', role=UserRole.Role.STANDARD
        )
        cell.allowed = False
        cell.save(update_fields=['allowed'])

        Feature.objects.filter(key='ads.access').delete()
        ensure_catalogue()

        # Recreated...
        self.assertTrue(Feature.objects.filter(key='ads.access').exists())
        # ...and the hand-made correction left alone. The database is
        # authoritative once seeded; a redeploy must not revert an operator.
        cell.refresh_from_db()
        self.assertFalse(cell.allowed)


class CheckDoesNotBlockItsOwnFixTests(TestCase):
    """`migrate` runs the system checks BEFORE applying anything.

    So an unseeded feature must not be an Error while migrations are pending -
    the first version of rbac/checks.py deadlocked exactly that way, refusing to
    let the migration that would seed a new feature run at all.
    """

    def test_a_missing_seed_row_is_ignored_while_migrations_are_pending(self):
        from unittest import mock

        from rbac import checks

        Feature.objects.filter(key='chat.access').delete()

        with mock.patch.object(checks, '_database_is_mid_upgrade', return_value=True):
            self.assertEqual(checks.registry_matches_database(None), [])

        # ...and is still reported once the database has caught up.
        with mock.patch.object(checks, '_database_is_mid_upgrade', return_value=False):
            errors = checks.registry_matches_database(None)

        self.assertEqual([e.id for e in errors], ['rbac.E001'])

    def test_an_uninspectable_database_reports_nothing(self):
        from unittest import mock

        from rbac import checks

        with mock.patch(
            'django.db.migrations.executor.MigrationExecutor',
            side_effect=RuntimeError('no connection'),
        ):
            self.assertTrue(checks._database_is_mid_upgrade())


class GrantAndRevokeTests(TestCase):
    """The service layer, where the guards live.

    In `rbac/services.py` rather than the viewset for the same reason
    `chat/membership.py` exists: a guard a management command or a migration
    can walk past is not a guard.
    """

    def setUp(self):
        self.actor = User.objects.create_user(username='root3', password='pw')
        services.grant(self.actor, UserRole.Role.ADMIN)
        self.target = User.objects.create_user(username='tess', password='pw')

    def test_granting_adds_the_row_and_records_who(self):
        services.grant(self.target, UserRole.Role.COMMERCIAL, by=self.actor)

        self.assertTrue(
            self.target.account_roles.filter(role='commercial').exists()
        )
        entry = RoleAuditLog.objects.filter(action='grant', role='commercial').get()
        self.assertEqual(entry.actor_label, 'root3')
        self.assertEqual(entry.subject_label, 'tess')

    def test_granting_twice_changes_nothing_and_logs_once(self):
        services.grant(self.target, UserRole.Role.COMMERCIAL, by=self.actor)
        services.grant(self.target, UserRole.Role.COMMERCIAL, by=self.actor)

        self.assertEqual(
            self.target.account_roles.filter(role='commercial').count(), 1
        )
        # A log full of no-ops is a log nobody reads.
        self.assertEqual(
            RoleAuditLog.objects.filter(action='grant', role='commercial').count(), 1
        )

    def test_revoking_removes_the_row_and_records_it(self):
        services.grant(self.target, UserRole.Role.COMMERCIAL, by=self.actor)
        services.revoke(self.target, UserRole.Role.COMMERCIAL, by=self.actor)

        self.assertFalse(
            self.target.account_roles.filter(role='commercial').exists()
        )
        self.assertTrue(RoleAuditLog.objects.filter(action='revoke').exists())

    def test_revoking_something_they_do_not_hold_is_a_no_op(self):
        self.assertIsNone(services.revoke(self.target, UserRole.Role.ADMIN))
        self.assertFalse(RoleAuditLog.objects.filter(action='revoke').exists())

    def test_the_last_account_admin_cannot_be_revoked(self):
        """Granting a role is itself an admin action.

        A product with no account admin has no route back to having one - that
        is a database restore, not a support ticket. Mirrors the last-admin
        guard in chat/membership.py:set_role.
        """
        with self.assertRaises(services.RoleChangeRefused):
            services.revoke(self.actor, UserRole.Role.ADMIN, by=self.actor)

        self.assertTrue(self.actor.account_roles.filter(role='admin').exists())

    def test_an_admin_may_step_down_once_somebody_else_holds_it(self):
        services.grant(self.target, UserRole.Role.ADMIN, by=self.actor)

        services.revoke(self.actor, UserRole.Role.ADMIN, by=self.actor)

        self.assertFalse(self.actor.account_roles.filter(role='admin').exists())
        self.assertTrue(self.target.account_roles.filter(role='admin').exists())

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(services.RoleChangeRefused):
            services.grant(self.target, 'wizard', by=self.actor)

    def test_the_log_survives_the_people_in_it(self):
        """SET_NULL keeps the row; the labels keep it readable.

        "granted admin to <nobody>" answers nothing, which is why the names are
        copied in as text at write time.
        """
        services.grant(self.target, UserRole.Role.COMMERCIAL, by=self.actor)

        self.target.delete()

        entry = RoleAuditLog.objects.filter(action='grant', role='commercial').get()
        self.assertIsNone(entry.subject_id)
        self.assertEqual(entry.subject_label, 'tess')

    def test_a_system_grant_records_no_actor(self):
        """Blank is a fact: a migration or the signup signal did it."""
        services.grant(self.target, UserRole.Role.COMMERCIAL)

        entry = RoleAuditLog.objects.filter(action='grant', role='commercial').get()
        self.assertIsNone(entry.actor_id)
        self.assertEqual(entry.actor_label, '')


class RoleApiTests(EnvelopeAssertions, TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='apiadmin', password='pw')
        self.admin.profile.email_verified = True
        self.admin.profile.save(update_fields=['email_verified'])
        services.grant(self.admin, UserRole.Role.ADMIN)

        self.plain = User.objects.create_user(username='apiplain', password='pw')
        self.plain.profile.email_verified = True
        self.plain.profile.save(update_fields=['email_verified'])

        self.client = APIClient()

    def _as(self, user):
        self.client.force_authenticate(user=user)
        return self.client

    def test_a_standard_user_cannot_even_list(self):
        self.assertFailure(
            self._as(self.plain).get('/api/v1/rbac/roles/'),
            status.HTTP_403_FORBIDDEN,
        )

    def test_an_admin_lists_every_role(self):
        data = self.assertSuccess(self._as(self.admin).get('/api/v1/rbac/roles/'))

        self.assertGreaterEqual(len(data['results']), 3)

    def test_an_admin_grants_and_the_target_gains_the_feature(self):
        self.assertSuccess(self._as(self.admin).post(
            '/api/v1/rbac/roles/grant/',
            {'user': self.plain.pk, 'role': 'commercial'}, format='json',
        ))

        self.assertTrue(can(self.plain, 'ads.access'))

    def test_a_standard_user_cannot_grant_themselves_anything(self):
        self.assertFailure(
            self._as(self.plain).post(
                '/api/v1/rbac/roles/grant/',
                {'user': self.plain.pk, 'role': 'admin'}, format='json',
            ),
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(can(self.plain, 'platform.role.grant'))

    def test_revoking_the_last_admin_is_a_400_not_a_403(self):
        """The caller IS allowed to revoke roles; this revocation is refused.

        A 403 would read as "you are not an admin", which is the opposite of
        what happened.
        """
        self.assertFailure(self._as(self.admin).post(
            '/api/v1/rbac/roles/revoke/',
            {'user': self.admin.pk, 'role': 'admin'}, format='json',
        ))

    def test_the_audit_endpoint_shows_what_happened(self):
        self._as(self.admin).post(
            '/api/v1/rbac/roles/grant/',
            {'user': self.plain.pk, 'role': 'commercial', 'note': 'signed up'},
            format='json',
        )

        data = self.assertSuccess(self._as(self.admin).get('/api/v1/rbac/roles/audit/'))
        entries = data['results']

        self.assertTrue(any(
            e['action'] == 'grant' and e['subject_label'] == 'apiplain'
            and e['note'] == 'signed up'
            for e in entries
        ))

    def test_a_standard_user_cannot_read_the_audit_log(self):
        self.assertFailure(
            self._as(self.plain).get('/api/v1/rbac/roles/audit/'),
            status.HTTP_403_FORBIDDEN,
        )


class AuthFlagsAreNotProductAccessTests(TestCase):
    """`is_staff` and `is_superuser` mean Django admin access and nothing else.

    Already true when rbac was written - a full audit found two references,
    both outside product code. This is what keeps it true, because the pull
    towards `if user.is_staff:` in the next privileged viewset is strong.
    """

    #: The two documented exceptions. `user/signals.py` uses is_superuser to
    #: let `createsuperuser` sign in without an email round trip;
    #: `user/admin.py` displays is_staff inside the Django admin itself.
    ALLOWED = {'user/signals.py', 'user/admin.py'}

    FORBIDDEN = (
        'is_staff', 'is_superuser', 'has_perm', 'IsAdminUser',
        'DjangoModelPermissions',
    )

    def test_no_product_module_reads_a_django_auth_flag(self):
        """Parsed, not grepped.

        A substring search cannot tell `user.is_staff` from a comment saying
        those flags are Django-admin-only - and rbac/models.py is full of the
        latter, deliberately. The AST sees code and nothing else, so the module
        that explains the rule does not fail it.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for app in ['chat', 'user', 'billing', 'enterprise', 'notification',
                    'sms', 'mail', 'otp', 'rbac', 'loopd', 'utils']:
            for path in (root / app).rglob('*.py'):
                rel = path.relative_to(root).as_posix()
                if (rel in self.ALLOWED or '/migrations/' in rel
                        or rel.endswith('tests.py')):
                    continue
                tree = ast.parse(path.read_text(), filename=rel)
                for node in ast.walk(tree):
                    name = None
                    if isinstance(node, ast.Attribute):
                        name = node.attr
                    elif isinstance(node, ast.Name):
                        name = node.id
                    if name in self.FORBIDDEN:
                        offenders.append(f'{rel}:{node.lineno}: {name}')

        self.assertEqual(
            offenders, [],
            'Product access comes from rbac, never from django.contrib.auth '
            'flags. Use can()/require() with a feature key.',
        )


class FeaturesEndpointTests(EnvelopeAssertions, TestCase):
    """`GET /api/v1/users/me/features/` - what a client renders menus from."""

    def setUp(self):
        self.user = User.objects.create_user(username='navuser', password='pw')
        self.user.profile.email_verified = True
        self.user.profile.save(update_fields=['email_verified'])
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_a_standard_account_gets_its_areas(self):
        data = self.assertSuccess(self.client.get('/api/v1/users/me/features/'))

        self.assertIn('chat.access', data['account'])
        self.assertIn('broadcast.access', data['account'])
        self.assertNotIn('ads.access', data['account'])
        self.assertNotIn('platform.role.grant', data['account'])

    def test_granting_commercial_adds_the_ads_area(self):
        services.grant(self.user, UserRole.Role.COMMERCIAL)

        data = self.assertSuccess(self.client.get('/api/v1/users/me/features/'))

        self.assertIn('ads.access', data['account'])

    def test_it_is_sorted_so_a_client_can_diff_it(self):
        data = self.assertSuccess(self.client.get('/api/v1/users/me/features/'))

        self.assertEqual(data['account'], sorted(data['account']))

    def test_signing_out_refuses(self):
        self.client.force_authenticate(user=None)

        response = self.client.get('/api/v1/users/me/features/')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_it_carries_no_conversation_features(self):
        """Those ride inline on the conversation, not here.

        A round trip per group would cost more than the boolean it replaces.
        """
        group = Conversation.start_group(self.user, 'Nav')

        data = self.assertSuccess(self.client.get('/api/v1/users/me/features/'))

        self.assertNotIn('group.message.send', data['account'])
        self.assertIn('group.message.send', str(group.pk) and
                      [f for f in features_for(self.user, group)])


class ConversationFeaturesInlineTests(EnvelopeAssertions, TestCase):
    """`features` on the conversation, which is what drives in-room controls."""

    def setUp(self):
        self.admin = User.objects.create_user(username='inlineadmin', password='pw')
        self.member = User.objects.create_user(username='inlinemember', password='pw')
        for u in (self.admin, self.member):
            u.profile.email_verified = True
            u.profile.save(update_fields=['email_verified'])
        self.group = Conversation.start_group(self.admin, 'Inline')
        self.group.add_participant(self.member)
        self.client = APIClient()

    def _fetch(self, user):
        self.client.force_authenticate(user=user)
        data = self.assertSuccess(
            self.client.get(f'/api/v1/chat/conversations/{self.group.pk}/')
        )
        return set(data['features'])

    def test_an_admin_sees_the_admin_features(self):
        features = self._fetch(self.admin)

        self.assertIn('group.member.remove', features)
        self.assertIn('group.settings.policy', features)
        self.assertIn('group.message.pin', features)

    def test_a_member_does_not(self):
        features = self._fetch(self.member)

        self.assertNotIn('group.member.remove', features)
        self.assertNotIn('group.settings.policy', features)
        self.assertIn('group.message.send', features)

    def test_the_settings_layer_shows_through(self):
        """Loosening a group setting changes what the member is offered."""
        self.assertNotIn('group.member.add', self._fetch(self.member))

        self.group.add_member_permission = Conversation.MemberAddPermission.MEMBERS
        self.group.save(update_fields=['add_member_permission'])

        self.assertIn('group.member.add', self._fetch(self.member))

    def test_it_agrees_with_is_admin(self):
        """Both are served; a client may move across one screen at a time."""
        self.client.force_authenticate(user=self.admin)
        data = self.assertSuccess(
            self.client.get(f'/api/v1/chat/conversations/{self.group.pk}/')
        )

        self.assertTrue(data['is_admin'])
        self.assertIn('group.settings.policy', data['features'])


class MatrixEndpointTests(EnvelopeAssertions, TestCase):
    """`GET /api/v1/rbac/matrix/` - what each role may use, grouped for reading."""

    def setUp(self):
        self.admin = User.objects.create_user(username='matrixadmin', password='pw')
        self.plain = User.objects.create_user(username='matrixplain', password='pw')
        for u in (self.admin, self.plain):
            u.profile.email_verified = True
            u.profile.save(update_fields=['email_verified'])
        services.grant(self.admin, UserRole.Role.ADMIN)
        self.client = APIClient()

    def _fetch(self, user):
        self.client.force_authenticate(user=user)
        return self.assertSuccess(self.client.get('/api/v1/rbac/matrix/'))

    def test_a_standard_user_cannot_read_it(self):
        self.client.force_authenticate(user=self.plain)

        self.assertFailure(
            self.client.get('/api/v1/rbac/matrix/'), status.HTTP_403_FORBIDDEN
        )

    def test_it_covers_every_feature_exactly_once(self):
        data = self._fetch(self.admin)

        keys = [
            f['key']
            for axis in data['axes']
            for area in axis['areas']
            for f in area['features']
        ]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), set(Feature.objects.values_list('key', flat=True)))

    def test_the_account_axis_reads_most_privileged_first(self):
        data = self._fetch(self.admin)
        account = next(a for a in data['axes'] if a['axis'] == 'account')

        self.assertEqual(account['roles'], ['admin', 'commercial', 'standard'])

    def test_roles_are_plain_strings(self):
        """They become JSON keys and dict lookups; a TextChoices member reprs
        as `UserRole.Role.ADMIN`, which is a debugging trap for no benefit."""
        data = self._fetch(self.admin)

        for axis in data['axes']:
            for role in axis['roles']:
                self.assertIs(type(role), str)

    def test_a_grant_is_reported_per_role(self):
        data = self._fetch(self.admin)
        account = next(a for a in data['axes'] if a['axis'] == 'account')
        ads = next(
            f for area in account['areas'] for f in area['features']
            if f['key'] == 'ads.access'
        )

        self.assertTrue(ads['grants']['commercial'])
        self.assertFalse(ads['grants']['standard'])

    def test_lockable_features_say_so(self):
        """'not allowed' and 'cannot be allowed' are different facts.

        The screen renders locked cells as an em dash rather than an unticked
        box, so a reader does not have to guess which one they are looking at.
        """
        data = self._fetch(self.admin)
        account = next(a for a in data['axes'] if a['axis'] == 'account')
        grant_roles = next(
            f for area in account['areas'] for f in area['features']
            if f['key'] == 'platform.role.grant'
        )

        self.assertTrue(grant_roles['locked'])

    def test_areas_group_and_stay_in_order(self):
        data = self._fetch(self.admin)
        account = next(a for a in data['axes'] if a['axis'] == 'account')
        areas = [a['area'] for a in account['areas']]

        self.assertEqual(areas, sorted(areas))
        self.assertEqual(len(areas), len(set(areas)))

    def test_a_missing_cell_reads_as_null_not_false(self):
        """A cell the seeder never wrote denies, exactly like False - but it
        means something different, and rbac/checks.py warns about it."""
        RoleFeature.objects.filter(
            feature__key='chat.access', role='commercial'
        ).delete()

        data = self._fetch(self.admin)
        account = next(a for a in data['axes'] if a['axis'] == 'account')
        chat = next(
            f for area in account['areas'] for f in area['features']
            if f['key'] == 'chat.access'
        )

        self.assertIsNone(chat['grants']['commercial'])

    def test_it_costs_a_bounded_number_of_queries(self):
        """One page load for the whole product's permissions.

        Cold: the caller's roles and their grants (the `require` gate), then
        the features and their cells. Warm it is three - the grants lookup is
        cached - so the cache is cleared to make the number mean something.
        """
        from django.core.cache import cache

        self.client.force_authenticate(user=self.admin)
        cache.clear()

        with self.assertNumQueries(4):
            self.client.get('/api/v1/rbac/matrix/')


class SetGrantTests(TestCase):
    """Editing one cell of the matrix, and the three guards that make it safe."""

    def setUp(self):
        self.actor = User.objects.create_user(username='editor', password='pw')
        services.grant(self.actor, UserRole.Role.ADMIN)

    def test_an_ordinary_grant_flips_and_is_audited(self):
        services.set_grant('ads.access', 'standard', True, by=self.actor)

        self.assertTrue(
            RoleFeature.objects.get(
                feature__key='ads.access', role='standard'
            ).allowed
        )
        entry = RoleAuditLog.objects.filter(action='feature.allow').get()
        self.assertEqual(entry.feature_key, 'ads.access')
        self.assertEqual(entry.role, 'standard')
        self.assertEqual(entry.actor_label, 'editor')

    def test_flipping_it_back_is_audited_as_a_denial(self):
        services.set_grant('ads.access', 'standard', True, by=self.actor)
        services.set_grant('ads.access', 'standard', False, by=self.actor)

        self.assertTrue(RoleAuditLog.objects.filter(action='feature.deny').exists())

    def test_setting_it_to_what_it_already_is_writes_nothing(self):
        before = RoleAuditLog.objects.count()

        services.set_grant('chat.access', 'standard', True, by=self.actor)

        self.assertEqual(RoleAuditLog.objects.count(), before)

    def test_a_lockable_feature_cannot_be_given_to_anybody_else(self):
        """Without this, the matrix screen IS the escalation path."""
        with self.assertRaises(services.RoleChangeRefused):
            services.set_grant(
                'platform.role.grant', 'standard', True, by=self.actor
            )

        self.assertFalse(
            RoleFeature.objects.get(
                feature__key='platform.role.grant', role='standard'
            ).allowed
        )

    def test_a_lockable_feature_cannot_be_taken_from_admin(self):
        """The mirror of chat's last-admin guard.

        Only admins may hold these, so denying admin means NOBODY holds it -
        and the feature that grants roles cannot be granted back. That is a
        database restore, not a support ticket.
        """
        with self.assertRaises(services.RoleChangeRefused):
            services.set_grant(
                'platform.role.grant', 'admin', False, by=self.actor
            )

        self.assertTrue(can(self.actor, 'platform.role.grant'))

    def test_the_edit_feature_cannot_lock_itself_away(self):
        """Editing the matrix is itself lockable, so it cannot be surrendered."""
        with self.assertRaises(services.RoleChangeRefused):
            services.set_grant(
                'platform.permissions.edit', 'admin', False, by=self.actor
            )

    def test_a_role_from_the_wrong_axis_is_refused(self):
        """`RoleFeature.role` is a CharField, so nothing at the database level
        stops `commercial` landing on a group feature. The row would match
        nothing and read like a real denial in the matrix."""
        with self.assertRaises(services.RoleChangeRefused):
            services.set_grant(
                'group.message.pin', 'commercial', True, by=self.actor
            )

    def test_an_unknown_feature_is_refused(self):
        with self.assertRaises(services.RoleChangeRefused):
            services.set_grant('nonsense.key', 'admin', True, by=self.actor)

    def test_the_change_takes_effect_immediately(self):
        """The grants cache must not outlive the grant it caches."""
        plain = User.objects.create_user(username='plain8', password='pw')
        self.assertFalse(can(plain, 'ads.access'))

        services.set_grant('ads.access', 'standard', True, by=self.actor)

        self.assertTrue(can(plain, 'ads.access'))


class SetGrantApiTests(EnvelopeAssertions, TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='editadmin', password='pw')
        self.plain = User.objects.create_user(username='editplain', password='pw')
        for u in (self.admin, self.plain):
            u.profile.email_verified = True
            u.profile.save(update_fields=['email_verified'])
        services.grant(self.admin, UserRole.Role.ADMIN)
        self.client = APIClient()

    def _post(self, user, payload):
        self.client.force_authenticate(user=user)
        return self.client.post('/api/v1/rbac/matrix/set/', payload, format='json')

    def test_an_admin_can_flip_a_cell_and_gets_the_matrix_back(self):
        data = self.assertSuccess(self._post(
            self.admin,
            {'feature': 'ads.access', 'role': 'standard', 'allowed': True},
        ))

        account = next(a for a in data['axes'] if a['axis'] == 'account')
        ads = next(
            f for area in account['areas'] for f in area['features']
            if f['key'] == 'ads.access'
        )
        self.assertTrue(ads['grants']['standard'])

    def test_a_standard_user_cannot_edit(self):
        self.assertFailure(
            self._post(
                self.plain,
                {'feature': 'ads.access', 'role': 'standard', 'allowed': True},
            ),
            status.HTTP_403_FORBIDDEN,
        )

    def test_viewing_does_not_imply_editing(self):
        """Separate features, so a support role could read without rewriting.

        `save()`, not `.update()`: a queryset update does not fire post_save,
        so the grants cache would keep serving the old answer. Written the long
        way here because that is exactly the trap this test fell into first.
        """
        cell = RoleFeature.objects.get(
            feature__key='platform.permissions.edit', role='admin'
        )
        cell.allowed = False
        cell.save(update_fields=['allowed'])

        self.assertFailure(
            self._post(
                self.admin,
                {'feature': 'ads.access', 'role': 'standard', 'allowed': True},
            ),
            status.HTTP_403_FORBIDDEN,
        )
        # ...and reading still works.
        self.client.force_authenticate(user=self.admin)
        self.assertSuccess(self.client.get('/api/v1/rbac/matrix/'))

    def test_a_refused_edit_is_a_400_not_a_403(self):
        """The caller IS allowed to edit; this edit is refused."""
        self.assertFailure(self._post(
            self.admin,
            {'feature': 'platform.role.grant', 'role': 'standard',
             'allowed': True},
        ))

    def test_the_audit_endpoint_shows_matrix_changes_too(self):
        self._post(
            self.admin,
            {'feature': 'ads.access', 'role': 'standard', 'allowed': True,
             'note': 'trial'},
        )

        self.client.force_authenticate(user=self.admin)
        data = self.assertSuccess(self.client.get('/api/v1/rbac/roles/audit/'))

        self.assertTrue(any(
            e['action'] == 'feature.allow' and e['feature_key'] == 'ads.access'
            and e['note'] == 'trial'
            for e in data['results']
        ))


class DirectConversationFeaturesTests(EnvelopeAssertions, TestCase):
    """`features` must say what the person can DO, not what a role grants.

    Pinning in a DM is the one place those differ: both sides are `member`, the
    floor denies it, and `Conversation.may_pin` allows it through a membership
    branch. A client reading the role answer would hide the pin control on
    every direct message.
    """

    def setUp(self):
        self.a = User.objects.create_user(username='dma', password='pw')
        self.b = User.objects.create_user(username='dmb', password='pw')
        for u in (self.a, self.b):
            u.profile.email_verified = True
            u.profile.save(update_fields=['email_verified'])
        self.direct, _ = Conversation.start_direct(self.a, self.b)
        self.client = APIClient()

    def _features(self, user):
        self.client.force_authenticate(user=user)
        data = self.assertSuccess(
            self.client.get(f'/api/v1/chat/conversations/{self.direct.pk}/')
        )
        return set(data['features'])

    def test_both_sides_may_pin_in_a_direct_conversation(self):
        for user in (self.a, self.b):
            self.assertIn('group.message.pin', self._features(user))
            self.assertIs(self.direct.may_pin(user), True)

    def test_it_agrees_with_may_pin_in_a_group(self):
        group = Conversation.start_group(self.a, 'Grp')
        group.add_participant(self.b)

        self.client.force_authenticate(user=self.b)
        data = self.assertSuccess(
            self.client.get(f'/api/v1/chat/conversations/{group.pk}/')
        )

        self.assertNotIn('group.message.pin', data['features'])
        self.assertIs(group.may_pin(self.b), False)

    def test_the_role_grant_itself_is_untouched(self):
        """The matrix still says members cannot pin - true in every group."""
        self.assertFalse(
            RoleFeature.objects.get(
                feature__key='group.message.pin', role='member'
            ).allowed
        )


class AdvertiserRoleRenameTests(TestCase):
    """rbac/0008 renames the account role `advertiser` to `commercial`.

    Its data step is called directly: by the time any test runs, every
    migration has already run over an empty database, so the case that matters
    - old rows beside fresh defaults - never happens on its own.
    """

    def setUp(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module(
            'rbac.migrations.0008_rename_advertiser_role_to_commercial'
        )
        self.rename = lambda: migration.forwards(apps, None)
        self.user = User.objects.create_user(username='shop', password='pw')

    def test_a_held_role_is_renamed(self):
        UserRole.objects.create(user=self.user, role='advertiser')

        self.rename()

        self.assertEqual(
            set(self.user.account_roles.values_list('role', flat=True)),
            {'standard', 'commercial'},
        )

    def test_an_operators_edit_to_the_old_role_beats_a_fresh_default(self):
        """0007's ensure_catalogue() may already have seeded `commercial`."""
        feature = Feature.objects.get(key='ads.access')
        self.assertTrue(
            RoleFeature.objects.get(feature=feature, role='commercial').allowed
        )
        RoleFeature.objects.create(feature=feature, role='advertiser', allowed=False)

        self.rename()

        self.assertFalse(
            RoleFeature.objects.get(feature=feature, role='commercial').allowed
        )
        self.assertFalse(RoleFeature.objects.filter(role='advertiser').exists())

    def test_the_audit_log_keeps_the_name_the_grant_was_made_under(self):
        RoleAuditLog.objects.create(action='grant', role='advertiser')

        self.rename()

        self.assertTrue(RoleAuditLog.objects.filter(role='advertiser').exists())
