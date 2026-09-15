"""Tests for the activity feed: which events are recorded, for whom, and what
the feed says about each.

Moved from chat/tests.py with the model. They still act through chat -
groups, memberships, the members endpoint - because that is where the events
come from; what they check is the row and the feed.
"""
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APITestCase

from chat import membership
from chat.models import Conversation, Participant
from chat.services import create_message
from chat.tests import make_user
from notification.models import Notification
from user.tests import EnvelopeAssertions


class ActivityNotificationTests(EnvelopeAssertions, APITestCase):
    """The events that used to exist only as a socket frame.

    Each of these already happened before this table; what is new is that it
    survives nobody being there to see it. The cases worth holding are the
    ones where a notification would be wrong rather than merely missing: your
    own actions, a phone-only member with no account to address, and one
    person's feed being reachable from another person's session.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.carol = make_user('carol')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)

    def feed(self, user, **params):
        self.client.force_authenticate(user)
        return self.assertSuccess(
            self.client.get(reverse('notification:notification-list'), params)
        )

    def types_for(self, user, **params):
        return [row['type'] for row in self.feed(user, **params)['results']]

    def bob_row(self):
        return Participant.objects.get(conversation=self.group, user=self.bob)

    # --- the events ---------------------------------------------------------

    def test_being_promoted_is_recorded(self):
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)

        self.assertIn('role.granted', self.types_for(self.bob))

    def test_being_demoted_is_recorded(self):
        row = self.bob_row()
        membership.set_role(row, Participant.Role.ADMIN, self.alice)
        membership.set_role(row, Participant.Role.MEMBER, self.alice)

        self.assertIn('role.revoked', self.types_for(self.bob))

    def test_being_removed_is_recorded(self):
        membership.remove(self.bob_row(), self.alice)

        self.assertIn('member.removed', self.types_for(self.bob))

    def test_being_added_is_recorded(self):
        # Through the endpoint an admin actually uses. Conversation
        # .add_participant is deliberately NOT the hook: it also runs for both
        # sides of a direct chat and for a group's own creator, neither of whom
        # wants telling they were added to something.
        self.client.force_authenticate(self.alice)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-members', args=[self.group.id]),
                {'members': ['carol']},
                format='json',
            )
        )

        self.assertIn('member.added', self.types_for(self.carol))

    # --- who does NOT get told ---------------------------------------------

    def test_you_are_not_told_about_your_own_action(self):
        # Alice demoting Alice would otherwise put "you are no longer an
        # admin" in the feed of the person who just clicked it.
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)
        alice_row = Participant.objects.get(
            conversation=self.group, user=self.alice
        )
        membership.set_role(alice_row, Participant.Role.MEMBER, self.alice)

        self.assertNotIn('role.revoked', self.types_for(self.alice))

    def test_a_phone_only_member_is_not_notified(self):
        # No account, so nothing to address - and the call must not raise
        # rather than every call site having to check for itself.
        mom = self.group.add_phone_participant(
            '+639171234567', display_name='Mom', added_by=self.alice
        )
        membership.remove(mom, self.alice)

        self.assertEqual(Notification.objects.filter(to_user=None).count(), 0)

    def test_a_message_never_creates_a_notification(self):
        # unread_count answers this. Two sources of truth would disagree.
        create_message(self.group, self.alice, text='hello')

        self.assertEqual(self.types_for(self.bob), [])

    # --- the feed itself ----------------------------------------------------

    def test_the_feed_is_only_your_own(self):
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)

        self.assertEqual(self.types_for(self.carol), [])

    def test_the_unread_count_rides_with_the_list(self):
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)

        self.assertEqual(self.feed(self.bob)['unread_count'], 1)

    def test_filtering_by_category(self):
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)

        self.assertEqual(self.types_for(self.bob, category='groups'),
                         ['role.granted'])
        self.assertEqual(self.types_for(self.bob, category='sms'), [])

    def test_marking_everything_read(self):
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)

        self.client.force_authenticate(self.bob)
        data = self.assertSuccess(
            self.client.post(reverse('notification:notification-read'), {})
        )

        self.assertEqual(data['marked'], 1)
        self.assertEqual(data['unread_count'], 0)
        self.assertEqual(self.feed(self.bob)['unread_count'], 0)

    def test_marking_read_cannot_reach_somebody_elses_row(self):
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)
        theirs = Notification.objects.get(to_user=self.bob)

        self.client.force_authenticate(self.carol)
        self.assertSuccess(
            self.client.post(
                reverse('notification:notification-read'), {'ids': [theirs.pk]},
                format='json',
            )
        )

        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)

    def test_unread_filter(self):
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)
        self.client.force_authenticate(self.bob)
        self.client.post(reverse('notification:notification-read'), {})

        self.assertEqual(self.types_for(self.bob, unread='1'), [])


class NotificationActionContextTests(EnvelopeAssertions, APITestCase):
    """What a client needs to decide what to OFFER on a notification.

    The row records what happened; the action has to be about what is true
    now. Somebody added and then removed must not still be handed a way in,
    and how a stranger gets into the group decides what "a way in" even is.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.join_mode = Conversation.JoinMode.CODE
        self.group.save(update_fields=['join_mode'])
        # Through the endpoint an admin uses. The model's add_participant is
        # deliberately not the hook - it also runs for both sides of a direct
        # chat and for a group's own creator.
        self.add_member(self.group, 'bob')

    def add_member(self, group, username):
        self.client.force_authenticate(self.alice)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-members', args=[group.id]),
                {'members': [username]},
                format='json',
            )
        )

    def feed(self, user):
        self.client.force_authenticate(user)
        return self.assertSuccess(
            self.client.get(reverse('notification:notification-list'))
        )['results']

    def test_the_join_mode_rides_along(self):
        self.assertEqual(
            self.feed(self.bob)[0]['conversation_join_mode'], 'code'
        )

    def test_a_member_reports_as_active(self):
        self.assertEqual(self.feed(self.bob)[0]['viewer_status'], 'active')

    def test_being_removed_afterwards_changes_the_answer(self):
        # The "you were added" notification stays - it did happen - but the
        # client must stop offering a door that is now shut.
        membership.remove(
            Participant.objects.get(conversation=self.group, user=self.bob),
            self.alice,
        )

        rows = self.feed(self.bob)
        added = [r for r in rows if r['type'] == 'member.added'][0]
        self.assertEqual(added['viewer_status'], 'removed')

    def test_the_whole_page_costs_one_query_for_membership(self):
        # Twenty notifications about twenty groups must not be twenty lookups.
        for index in range(5):
            group = Conversation.start_group(self.alice, name=f'G{index}')
            self.add_member(group, 'bob')

        self.client.force_authenticate(self.bob)
        url = reverse('notification:notification-list')
        with CaptureQueriesContext(connection) as first:
            self.client.get(url)

        for index in range(5, 10):
            group = Conversation.start_group(self.alice, name=f'G{index}')
            self.add_member(group, 'bob')

        with CaptureQueriesContext(connection) as second:
            self.client.get(url)

        self.assertEqual(len(second), len(first))


class JoinRequestNotificationTests(EnvelopeAssertions, APITestCase):
    """An admin should be able to act on a request from the notification.

    Which means the row has to say WHICH membership is asking. Every review
    endpoint is keyed on Participant.pk, and that is not the user's id - they
    differ as soon as somebody is in more than one group, and a phone-only
    requester has no user id at all.
    """

    def setUp(self):
        self.admin = make_user('alice')
        self.stranger = make_user('bob')
        self.group = Conversation.start_group(self.admin, name='Pickle')
        self.group.join_mode = Conversation.JoinMode.APPROVAL
        self.group.save(update_fields=['join_mode'])

    def test_the_admin_is_told_which_membership_is_asking(self):
        membership.request_to_join(self.group, user=self.stranger)

        self.client.force_authenticate(self.admin)
        rows = self.assertSuccess(
            self.client.get(reverse('notification:notification-list'))
        )['results']
        asking = [r for r in rows if r['type'] == 'join.requested'][0]

        pending = Participant.objects.get(
            conversation=self.group, user=self.stranger
        )
        self.assertEqual(asking['data']['participant_id'], pending.pk)

    def test_that_id_is_what_the_review_endpoint_takes(self):
        membership.request_to_join(self.group, user=self.stranger)
        pending = Participant.objects.get(
            conversation=self.group, user=self.stranger
        )

        self.client.force_authenticate(self.admin)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-requests', args=[self.group.id]),
                {'participant': pending.pk, 'decision': 'approve'},
                format='json',
            )
        )

        pending.refresh_from_db()
        self.assertEqual(pending.status, Participant.Status.ACTIVE)

    def test_the_feed_says_whether_a_request_is_still_open(self):
        # A notification is a record, not a queue. Once the request behind it
        # has been answered - here, or from the admin panel, or by another
        # admin - the client has to stop offering to answer it again.
        membership.request_to_join(self.group, user=self.stranger)
        pending = Participant.objects.get(
            conversation=self.group, user=self.stranger
        )

        self.client.force_authenticate(self.admin)
        rows = self.assertSuccess(
            self.client.get(reverse('notification:notification-list'))
        )['results']
        asking = [r for r in rows if r['type'] == 'join.requested'][0]
        self.assertEqual(asking['request_status'], 'pending')

        membership.approve(pending, self.admin)

        rows = self.assertSuccess(
            self.client.get(reverse('notification:notification-list'))
        )['results']
        asking = [r for r in rows if r['type'] == 'join.requested'][0]
        self.assertEqual(asking['request_status'], 'active')
