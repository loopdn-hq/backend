"""Tests for the activity feed: which events are recorded, for whom, and what
the feed says about each.

Moved from chat/tests.py with the model. They still act through chat -
groups, memberships, the members endpoint - because that is where the events
come from; what they check is the row and the feed.
"""
from contextlib import contextmanager
from unittest import mock

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APITestCase

from chat import membership
from chat.models import Conversation, Message, Participant
from chat.services import create_message
from chat.tests import make_user
from notification.models import Notification, PushSubscription
from notification.push import payloads, rules
from user.models import NotificationSettings
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
        # 'sms' is no longer a chip: text and email failures share 'delivery'.
        self.assertEqual(self.types_for(self.bob, category='delivery'), [])
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


# --- web push --------------------------------------------------------------
#
# The other half of this app: the activity feed above records what happened,
# and push is how it reaches somebody whose browser is closed.
#
# `capture_push()` patches `send` - the single function that touches the
# network - so everything above it runs for real: the audience query, the block
# screen, the mention lookup, the rules and the payload. Same idea as
# mail/tests.py:capture_sent().

# Enough configuration for push to consider itself switched on. The keys are
# never used: `send` is patched everywhere except DeadSubscriptionTests, which
# patches the provider call instead.
PUSH_ON = override_settings(
    PUSH_SENDING_ENABLED=True,
    VAPID_PUBLIC_KEY='test-public-key',
    VAPID_PRIVATE_KEY='test-private-key',
    VAPID_SUBJECT='mailto:test@loopdn.com',
)


def subscribe(user, endpoint=None):
    """Give this account a browser to be notified on."""
    return PushSubscription.objects.create(
        user=user,
        endpoint=endpoint or f'https://push.example.com/{user.username}',
        p256dh='a-public-key',
        auth='a-secret',
        user_agent='Test Browser',
    )


@contextmanager
def capture_push():
    """Everything push tried to send, without a network call."""
    sent = []

    def record(subscription, payload):
        sent.append((subscription, payload))
        return True

    with mock.patch('notification.push.outbound.send', side_effect=record):
        yield sent


class PushRulesTests(TestCase):
    """Which switch decides which alert. push/rules.py is the only reader."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )
        self.prefs = NotificationSettings.for_user(self.bob)
        self.message = create_message(self.group, self.alice, text='Dinner at 7')

    def alert(self, **kwargs):
        return rules.message_alert(self.bob_row, self.message, **kwargs)

    def test_a_group_message_is_a_group_alert(self):
        self.assertEqual(self.alert(), (rules.GROUP, None))

    def test_messages_off_silences_everything(self):
        self.prefs.messages = False
        self.prefs.save()

        self.assertEqual(self.alert(), (None, 'messages off'))
        # Including a message that names them: the main switch is the main
        # switch.
        self.assertEqual(self.alert(mentioned=True), (None, 'messages off'))

    def test_group_messages_off_still_lets_a_mention_through(self):
        self.prefs.group_messages = False
        self.prefs.save()

        self.assertEqual(self.alert(), (None, 'group messages off'))
        self.assertEqual(self.alert(mentioned=True), (rules.MENTION, None))
        self.assertEqual(self.alert(replied_to=True), (rules.REPLY, None))

    def test_mentions_off_leaves_the_ordinary_alert_alone(self):
        """Turning off the SPECIAL treatment does not remove the normal one."""
        self.prefs.mentions_replies = False
        self.prefs.save()

        self.assertEqual(self.alert(mentioned=True), (rules.GROUP, None))

    def test_mentions_off_and_group_off_is_silence(self):
        self.prefs.mentions_replies = False
        self.prefs.group_messages = False
        self.prefs.save()

        self.assertEqual(self.alert(mentioned=True), (None, 'group messages off'))

    def test_a_direct_message_asks_the_direct_switch(self):
        direct, _ = Conversation.start_direct(self.alice, self.bob)
        message = create_message(direct, self.alice, text='Hello')
        row = Participant.objects.get(conversation=direct, user=self.bob)

        self.assertEqual(
            rules.message_alert(row, message), (rules.DIRECT, None)
        )

        self.prefs.direct_messages = False
        self.prefs.save()
        # Re-read the membership: a Participant loaded once caches the user it
        # points at, and that user caches its settings row. A fan-out loads its
        # rows fresh, so only a test can see the stale copy.
        row = Participant.objects.get(conversation=direct, user=self.bob)
        self.assertEqual(
            rules.message_alert(row, message), (None, 'direct messages off')
        )

    def test_somebody_with_no_settings_row_gets_the_defaults(self):
        """A fan-out must never write a settings row per recipient."""
        NotificationSettings.objects.filter(user=self.bob).delete()
        self.bob_row.refresh_from_db()

        self.assertEqual(self.alert(), (rules.GROUP, None))
        self.assertFalse(
            NotificationSettings.objects.filter(user=self.bob).exists()
        )

    def test_only_some_activity_events_are_pushes(self):
        added = Notification.objects.create(
            to_user=self.bob,
            from_user=self.alice,
            type=Notification.Type.MEMBER_ADDED,
            conversation=self.group,
        )
        promoted = Notification.objects.create(
            to_user=self.bob,
            from_user=self.alice,
            type=Notification.Type.ROLE_GRANTED,
            conversation=self.group,
        )

        self.assertEqual(rules.notification_alert(added), (True, None))
        allowed, reason = rules.notification_alert(promoted)
        self.assertFalse(allowed)
        self.assertIn('not a push', reason)

    def test_getting_into_a_loop_is_pushed_three_ways(self):
        """Added, let in after asking, and - for an admin - asked."""
        for event in (
            Notification.Type.MEMBER_ADDED,
            Notification.Type.JOIN_APPROVED,
            Notification.Type.JOIN_REQUESTED,
        ):
            row = Notification.objects.create(
                to_user=self.bob,
                from_user=self.alice,
                type=event,
                conversation=self.group,
            )
            self.assertEqual(
                rules.notification_alert(row), (True, None), event
            )

    def test_every_pushed_event_has_words(self):
        """A type that is pushed with no wording arrives blank - and a blank
        notification is the one failure nobody reports, because it looks like
        the feature simply did not fire."""
        self.assertEqual(
            set(rules.SETTING_BY_NOTIFICATION_TYPE),
            set(payloads.EVENT_WORDS),
        )

    def test_each_event_reads_as_itself(self):
        cases = {
            Notification.Type.MEMBER_ADDED: 'added you to Family',
            Notification.Type.JOIN_APPROVED: 'approved your request to join',
            Notification.Type.JOIN_REQUESTED: 'asked to join Family',
        }
        for event, expected in cases.items():
            row = Notification.objects.create(
                to_user=self.bob,
                from_user=self.alice,
                type=event,
                conversation=self.group,
            )
            self.assertIn(expected, payloads.for_notification(row)['body'])

    def test_the_preview_switch_covers_events_too(self):
        # `self.prefs`, not a fresh for_user(): the notification below carries
        # this very User object, which already has its settings cached on it.
        # A fan-out re-reads the row (dispatch_notification select_relates it),
        # so only a test can see the stale copy.
        self.prefs.show_preview = False
        self.prefs.save()

        row = Notification.objects.create(
            to_user=self.bob,
            from_user=self.alice,
            type=Notification.Type.JOIN_REQUESTED,
            conversation=self.group,
        )
        body = payloads.for_notification(row)['body']
        self.assertNotIn('Family', body)
        self.assertNotIn('alice', body.lower())

    def test_group_invitations_off_silences_being_added(self):
        self.prefs.group_invitations = False
        self.prefs.save()

        invitations = [
            event
            for event, field in rules.SETTING_BY_NOTIFICATION_TYPE.items()
            if field == 'group_invitations'
        ]
        self.assertEqual(len(invitations), 3)

        for event in invitations:
            row = Notification.objects.create(
                to_user=self.bob, type=event, conversation=self.group
            )
            self.assertEqual(
                rules.notification_alert(row),
                (False, 'group_invitations off'),
                event,
            )

    # --- what it says ------------------------------------------------------

    def test_the_preview_switch_withholds_the_words(self):
        prefs = NotificationSettings.for_user(self.bob)
        prefs.show_preview = False
        prefs.save()

        payload = payloads.for_message(self.message, self.bob_row, rules.GROUP)
        self.assertNotIn('Dinner', payload['body'])
        self.assertNotIn('alice', payload['body'].lower())

    def test_with_the_preview_on_it_names_the_sender_and_the_words(self):
        payload = payloads.for_message(self.message, self.bob_row, rules.GROUP)

        self.assertEqual(payload['title'], 'Family')
        self.assertIn('Dinner at 7', payload['body'])
        self.assertEqual(payload['url'], f'/chats/#conversation={self.group.pk}')

    def test_sound_none_is_a_silent_notification(self):
        prefs = NotificationSettings.for_user(self.bob)
        prefs.sound = NotificationSettings.Sound.NONE
        prefs.save()

        payload = payloads.for_message(self.message, self.bob_row, rules.GROUP)
        self.assertTrue(payload['silent'])

@PUSH_ON
class PushFanOutTests(TestCase):
    """Who is notified about a message, and who is not."""

    def setUp(self):
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.carol = make_user('carol', '+639170000003')
        self.group = Conversation.start_group(self.alice, name='Family')
        for account in (self.bob, self.carol):
            self.group.add_participant(account)
        for account in (self.alice, self.bob, self.carol):
            subscribe(account)

    def recipients(self, message):
        from notification.push.outbound import fan_out

        with capture_push() as sent:
            fan_out(message)
        return sorted(subscription.user.username for subscription, _ in sent)

    def test_everybody_but_the_sender(self):
        message = create_message(self.group, self.alice, text='Dinner at 7')

        self.assertEqual(self.recipients(message), ['bob', 'carol'])

    def test_a_muted_loop_stays_silent(self):
        row = Participant.objects.get(conversation=self.group, user=self.bob)
        row.muted = True
        row.save(update_fields=['muted'])

        message = create_message(self.group, self.alice, text='Dinner at 7')
        self.assertEqual(self.recipients(message), ['carol'])

    def test_a_mention_does_not_break_through_a_mute(self):
        """Yul's call: mute means no interruptions from that Loop at all."""
        row = Participant.objects.get(conversation=self.group, user=self.bob)
        row.muted = True
        row.save(update_fields=['muted'])

        message = create_message(
            self.group, self.alice, text='@bob', mentions=[row.pk]
        )
        self.assertEqual(self.recipients(message), ['carol'])

    def test_a_blocked_sender_notifies_nobody_who_blocked_them(self):
        from user import blocks

        blocks.block(self.bob, user=self.alice)

        message = create_message(self.group, self.alice, text='Dinner at 7')
        self.assertEqual(self.recipients(message), ['carol'])

    def test_a_member_who_left_is_not_notified(self):
        row = Participant.objects.get(conversation=self.group, user=self.carol)
        row.status = Participant.Status.LEFT
        row.save(update_fields=['status'])

        message = create_message(self.group, self.alice, text='Dinner at 7')
        self.assertEqual(self.recipients(message), ['bob'])

    def test_a_phone_only_member_is_skipped(self):
        """They get the text itself; there is no browser to tap."""
        self.group.add_phone_participant(
            '+639170000009', display_name='Dad', added_by=self.alice
        )

        message = create_message(self.group, self.alice, text='Dinner at 7')
        self.assertEqual(self.recipients(message), ['bob', 'carol'])

    def test_somebody_with_no_browser_registered_gets_nothing(self):
        PushSubscription.objects.filter(user=self.carol).delete()

        message = create_message(self.group, self.alice, text='Dinner at 7')
        self.assertEqual(self.recipients(message), ['bob'])

    def test_every_browser_of_the_same_person_is_notified(self):
        subscribe(self.bob, endpoint='https://push.example.com/bob-phone')

        message = create_message(self.group, self.alice, text='Dinner at 7')
        self.assertEqual(self.recipients(message), ['bob', 'bob', 'carol'])

    def test_a_deleted_message_notifies_nobody(self):
        from django.utils import timezone

        message = create_message(self.group, self.alice, text='Oops')
        message.deleted_at = timezone.now()
        message.save(update_fields=['deleted_at'])

        self.assertEqual(self.recipients(message), [])

    def test_the_alert_says_which_kind_it_is(self):
        from notification.push.outbound import fan_out

        row = Participant.objects.get(conversation=self.group, user=self.bob)
        message = create_message(
            self.group, self.alice, text='@bob dinner?', mentions=[row.pk]
        )

        with capture_push() as sent:
            fan_out(message)

        kinds = {
            subscription.user.username: payload['data']['kind']
            for subscription, payload in sent
        }
        self.assertEqual(kinds['bob'], rules.MENTION)
        self.assertEqual(kinds['carol'], rules.GROUP)

@PUSH_ON
class PushWiredUpTests(TransactionTestCase):
    """The whole path: create_message, commit, fan-out, wire.

    TransactionTestCase because the fan-out is scheduled with
    `transaction.on_commit`, which a plain TestCase never reaches.
    """

    def setUp(self):
        # Push is really wired up in this class, so anything that creates a
        # membership - `start_group` here, `start_direct` in a test below -
        # reaches the provider for real and logs a failure with the fake key.
        # Patched for the whole class; the tests that want to SEE what was sent
        # patch again on top with capture_push().
        quiet = mock.patch('notification.push.outbound.send', return_value=True)
        quiet.start()
        self.addCleanup(quiet.stop)

        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        subscribe(self.bob)

    def test_sending_a_message_notifies_the_other_member(self):
        with capture_push() as sent:
            create_message(self.group, self.alice, text='Dinner at 7')

        self.assertEqual(len(sent), 1)
        subscription, payload = sent[0]
        self.assertEqual(subscription.user, self.bob)
        self.assertIn('Dinner at 7', payload['body'])

    def test_a_direct_message_is_pushed_though_it_is_never_texted(self):
        """The one way push differs from SMS and email."""
        direct, _ = Conversation.start_direct(self.alice, self.bob)

        with capture_push() as sent:
            create_message(direct, self.alice, text='Just you')

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]['title'], self.alice.profile.display_label)

    def test_the_other_channels_still_refuse_a_direct_message(self):
        from chat import delivery
        from chat.models import ChannelConsent

        direct, _ = Conversation.start_direct(self.alice, self.bob)
        message = create_message(direct, self.alice, text='Just you')

        for channel in (ChannelConsent.Channel.SMS, ChannelConsent.Channel.EMAIL):
            self.assertEqual(delivery.eligible_for(message, channel), ([], []))

    def test_push_off_sends_nothing(self):
        with override_settings(PUSH_SENDING_ENABLED=False):
            with capture_push() as sent:
                create_message(self.group, self.alice, text='Dinner at 7')

        self.assertEqual(sent, [])

    def test_being_added_to_a_loop_is_pushed(self):
        carol = make_user('carol')
        subscribe(carol)

        with capture_push() as sent:
            self.group.add_participant(carol)
            from chat.services import announce_added_to_conversation

            announce_added_to_conversation(self.group, [carol])

        self.assertEqual(len(sent), 1)
        self.assertIn('added you', sent[0][1]['body'])

    def test_a_push_that_fails_does_not_fail_the_message(self):
        """The message is already stored and on everybody's screen."""
        with mock.patch(
            'notification.push.outbound.send', side_effect=RuntimeError('provider down')
        ):
            message = create_message(self.group, self.alice, text='Dinner at 7')

        self.assertTrue(Message.objects.filter(pk=message.pk).exists())

@PUSH_ON
class PushSubscriptionApiTests(APITestCase):
    """A browser registering, moving account, and unregistering."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.payload = {
            'endpoint': 'https://push.example.com/abc',
            'keys': {'p256dh': 'a-public-key', 'auth': 'a-secret'},
            'user_agent': 'Test Browser',
        }

    def test_the_key_endpoint_serves_the_public_half_only(self):
        self.client.force_authenticate(self.alice)

        response = self.client.get(reverse('push:key'))
        body = response.json()['data']

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['public_key'], 'test-public-key')
        self.assertTrue(body['enabled'])
        self.assertNotIn('private_key', body)

    def test_signing_in_is_required(self):
        response = self.client.post(
            reverse('push:subscribe'), self.payload, format='json'
        )
        self.assertEqual(response.status_code, 401)

    def test_a_browser_registers_once(self):
        self.client.force_authenticate(self.alice)

        self.client.post(reverse('push:subscribe'), self.payload, format='json')
        self.client.post(reverse('push:subscribe'), self.payload, format='json')

        self.assertEqual(PushSubscription.objects.count(), 1)

    def test_a_shared_browser_moves_to_whoever_signed_in(self):
        self.client.force_authenticate(self.alice)
        self.client.post(reverse('push:subscribe'), self.payload, format='json')

        self.client.force_authenticate(self.bob)
        self.client.post(reverse('push:subscribe'), self.payload, format='json')

        self.assertEqual(PushSubscription.objects.count(), 1)
        self.assertEqual(PushSubscription.objects.get().user, self.bob)

    def test_a_subscription_without_keys_is_refused(self):
        self.client.force_authenticate(self.alice)

        response = self.client.post(
            reverse('push:subscribe'),
            {'endpoint': 'https://push.example.com/abc', 'keys': {}},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_turning_notifications_off_forgets_the_browser(self):
        self.client.force_authenticate(self.alice)
        self.client.post(reverse('push:subscribe'), self.payload, format='json')

        response = self.client.post(
            reverse('push:unsubscribe'),
            {'endpoint': self.payload['endpoint']},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_nobody_can_unsubscribe_somebody_elses_browser(self):
        self.client.force_authenticate(self.alice)
        self.client.post(reverse('push:subscribe'), self.payload, format='json')

        self.client.force_authenticate(self.bob)
        self.client.post(
            reverse('push:unsubscribe'),
            {'endpoint': self.payload['endpoint']},
            format='json',
        )

        self.assertEqual(PushSubscription.objects.count(), 1)

@PUSH_ON
class DeadSubscriptionTests(TestCase):
    """A browser that has been wiped is forgotten at the first refusal."""

    def setUp(self):
        self.alice = make_user('alice')
        self.subscription = subscribe(self.alice)

    def fail_with(self, status):
        from pywebpush import WebPushException

        response = mock.Mock()
        response.status_code = status
        return WebPushException('refused', response=response)

    def test_a_gone_subscription_is_deleted(self):
        from notification.push.outbound import send

        with mock.patch('pywebpush.webpush', side_effect=self.fail_with(410)):
            delivered = send(self.subscription, {'title': 'hi'})

        self.assertFalse(delivered)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_a_bad_minute_at_the_provider_keeps_the_subscription(self):
        from notification.push.outbound import send

        with mock.patch('pywebpush.webpush', side_effect=self.fail_with(500)):
            delivered = send(self.subscription, {'title': 'hi'})

        self.assertFalse(delivered)
        self.assertEqual(PushSubscription.objects.count(), 1)

    def test_a_successful_send_records_when(self):
        from notification.push.outbound import send

        with mock.patch('pywebpush.webpush', return_value=None):
            self.assertTrue(send(self.subscription, {'title': 'hi'}))

        self.subscription.refresh_from_db()
        self.assertIsNotNone(self.subscription.last_used_at)


@PUSH_ON
class DeliveryAlertTests(TestCase):
    """"Your message did not reach everyone."

    `captureOnCommitCallbacks` where a push is expected: the alert is pushed
    from `transaction.on_commit`, which a plain TestCase never reaches.

    One alert per message per channel, told to the SENDER - see
    notification/delivery_alerts.py for why it is not one per person.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.message = create_message(self.group, self.alice, text='Dinner at 7')
        subscribe(self.alice)

    def announce(self, count, type=Notification.Type.SMS_SEND_FAILED):
        from notification.delivery_alerts import announce_failure

        return announce_failure(self.message, type, count)

    def test_the_sender_is_told_how_many_missed_it(self):
        row = self.announce(4)

        self.assertEqual(row.to_user, self.alice)
        self.assertEqual(row.data['count'], 4)
        self.assertIsNone(row.from_user)

    def test_more_failures_update_the_same_alert(self):
        """A second buzz about one message says nothing new."""
        first = self.announce(2)
        again = self.announce(5)

        self.assertEqual(Notification.objects.filter(message=self.message).count(), 1)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.data['count'], 5)

    def test_nothing_to_tell_when_the_sender_has_no_account(self):
        texted_in = create_message(
            self.group, None, text='From a handset',
            participant=Participant.objects.get(
                conversation=self.group, user=self.bob
            ),
        )
        from notification.delivery_alerts import announce_failure

        self.assertIsNone(
            announce_failure(
                texted_in, Notification.Type.SMS_SEND_FAILED, 3
            )
        )

    def test_a_count_of_zero_is_not_an_alert(self):
        self.assertIsNone(self.announce(0))

    def test_it_reaches_the_sender_as_a_push(self):
        with capture_push() as sent, self.captureOnCommitCallbacks(execute=True):
            self.announce(2)

        self.assertEqual(len(sent), 1)
        self.assertIn('2 people did not get your message', sent[0][1]['body'])
        self.assertIn('Family', sent[0][1]['body'])

    def test_one_person_reads_as_one_person(self):
        with capture_push() as sent, self.captureOnCommitCallbacks(execute=True):
            self.announce(1)

        self.assertIn('1 person did not get your message', sent[0][1]['body'])

    def test_the_switch_silences_it(self):
        prefs = NotificationSettings.for_user(self.alice)
        prefs.sms_delivery_failures = False
        prefs.save()

        with capture_push() as sent, self.captureOnCommitCallbacks(execute=True):
            self.announce(2)

        self.assertEqual(sent, [])
        # Still recorded in the feed: the switch is about the interruption.
        self.assertEqual(
            Notification.objects.filter(message=self.message).count(), 1
        )

    def test_each_channel_has_its_own_switch(self):
        prefs = NotificationSettings.for_user(self.alice)
        prefs.sms_delivery_failures = False
        prefs.save()

        with capture_push() as sent, self.captureOnCommitCallbacks(execute=True):
            self.announce(2, Notification.Type.EMAIL_SEND_FAILED)

        self.assertEqual(len(sent), 1)
        self.assertIn('by email', sent[0][1]['body'])

    def test_both_channels_share_the_delivery_chip(self):
        for type in (
            Notification.Type.SMS_SEND_FAILED,
            Notification.Type.EMAIL_SEND_FAILED,
        ):
            row = Notification(to_user=self.alice, type=type)
            self.assertEqual(row.category, 'delivery')
