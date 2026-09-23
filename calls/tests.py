"""Tests for calls: who may start, join and end one, the plan's participant
limit, recording, LiveKit's webhook, and the socket frames.

LiveKit and S3 are never contacted. Tokens are real - they are signed locally,
and decoding one is how these tests check what it lets somebody do - but every
call that would leave the process (`calls.livekit.start_recording`,
`calls.storage.download_url`, ...) is patched at that seam.
"""
import base64
import hashlib
import json
from datetime import timedelta
from unittest import mock

import jwt
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from livekit import api as lk
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from billing.models import Plan, Subscription
from calls import services
from calls.livekit import CallServiceError
from calls.models import CallEvent, CallParticipant, CallRecording, CallRoom
from chat.models import Conversation, Participant
from user.tests import EnvelopeAssertions

API_KEY = 'devkey'
API_SECRET = 'a-livekit-secret-that-is-long-enough-for-hs256'

LIVEKIT = dict(
    LIVEKIT_URL='wss://livekit.test',
    LIVEKIT_API_KEY=API_KEY,
    LIVEKIT_API_SECRET=API_SECRET,
    LIVEKIT_TOKEN_TTL_SECONDS=600,
    LIVEKIT_RECORDING_BUCKET='recordings-bucket',
    LIVEKIT_RECORDING_PREFIX='call-recordings',
)


def make_user(username):
    # No password: these tests authenticate directly, and hashing one is by far
    # the slowest thing a setUp here could do.
    return User.objects.create_user(username=username, email=f'{username}@example.com')


def put_on_plan(user, code):
    Subscription.objects.update_or_create(
        user=user, defaults={'plan': Plan.objects.get(code=code)}
    )


def decode(token):
    return jwt.decode(
        token, API_SECRET, algorithms=['HS256'], issuer=API_KEY, options={'verify_aud': False}
    )


def egress(status, egress_id='EG_1', **kwargs):
    return lk.EgressInfo(egress_id=egress_id, status=status, **kwargs)


@override_settings(**LIVEKIT)
class CallTestBase(EnvelopeAssertions, APITestCase):
    """A group of alice (its owner and admin), bob, carol, and a direct thread."""

    def setUp(self):
        cache.clear()  # throttles live in the cache, which outlives a test
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.carol = make_user('carol')
        self.dave = make_user('dave')

        self.group = Conversation.start_group(self.alice, name='Family')
        for user in (self.bob, self.carol):
            self.group.add_participant(user)
        self.direct, _ = Conversation.start_direct(self.alice, self.bob)

        self.client.force_authenticate(self.alice)

    def as_user(self, user):
        self.client.force_authenticate(user)

    def start(self, conversation=None, call_type='audio', user=None):
        if user is not None:
            self.as_user(user)
        return self.client.post(
            reverse('calls:call-list'),
            {'conversation': (conversation or self.group).pk, 'call_type': call_type},
            format='json',
        )

    def start_ok(self, **kwargs):
        return self.assertSuccess(self.start(**kwargs), 201)

    def url(self, name, call_id):
        return reverse(f'calls:call-{name}', args=[call_id])

    def post(self, name, call_id, user=None, **kwargs):
        if user is not None:
            self.as_user(user)
        return self.client.post(self.url(name, call_id), kwargs, format='json')

    def join(self, call_id, user):
        return self.post('join', call_id, user=user)


# -- starting a call -----------------------------------------------------------


class StartCallTests(CallTestBase):
    def test_a_member_starts_a_call_and_is_in_it(self):
        data = self.start_ok()

        self.assertEqual(data['call']['status'], 'ringing')
        self.assertEqual(data['call']['call_type'], 'audio')
        self.assertEqual(data['call']['created_by']['id'], self.alice.pk)
        self.assertEqual(data['call']['participant_count'], 1)
        self.assertEqual(data['livekit']['url'], 'wss://livekit.test')

        call = CallRoom.objects.get()
        self.assertEqual(call.conversation, self.group)
        stay = call.participants.get()
        self.assertEqual(stay.user, self.alice)
        self.assertIsNone(stay.left_at)

    def test_login_is_required(self):
        self.client.force_authenticate(None)
        response = self.start()
        self.assertEqual(response.status_code, 401)
        self.assertFalse(CallRoom.objects.exists())

    def test_the_token_is_scoped_to_one_room_and_one_person(self):
        data = self.start_ok()
        claims = decode(data['livekit']['token'])
        call = CallRoom.objects.get()

        self.assertEqual(claims['sub'], str(self.alice.pk))
        self.assertEqual(claims['video']['room'], call.room_name)
        self.assertTrue(claims['video']['roomJoin'])
        # Taking part and nothing more.
        for admin_grant in ('roomAdmin', 'roomCreate', 'roomRecord', 'roomList', 'hidden'):
            self.assertFalse(claims['video'].get(admin_grant), admin_grant)

    def test_the_token_is_short_lived(self):
        data = self.start_ok()
        claims = decode(data['livekit']['token'])
        self.assertLessEqual(claims['exp'] - claims['nbf'], 600)

    def test_the_api_secret_never_leaves_the_server(self):
        response = self.start()
        self.assertNotIn(API_SECRET, response.content.decode())

    def test_an_audio_call_cannot_publish_video(self):
        data = self.start_ok(call_type='audio')
        sources = decode(data['livekit']['token'])['video']['canPublishSources']
        self.assertIn('microphone', sources)
        self.assertNotIn('camera', sources)

    def test_a_video_call_can(self):
        data = self.start_ok(call_type='video')
        sources = decode(data['livekit']['token'])['video']['canPublishSources']
        self.assertIn('camera', sources)

    def test_screen_sharing_follows_the_plan(self):
        # alice owns the group, and is on Free: no screen share.
        sources = decode(self.start_ok()['livekit']['token'])['video']['canPublishSources']
        self.assertNotIn('screen_share', sources)

        call = CallRoom.objects.get()
        services.end_call(self.alice, call.pk)

        put_on_plan(self.alice, 'premium')
        sources = decode(self.start_ok()['livekit']['token'])['video']['canPublishSources']
        self.assertIn('screen_share', sources)

    def test_somebody_outside_the_conversation_cannot_start_one(self):
        response = self.start(user=self.dave)
        # 404, not 403: the conversation must not be discoverable.
        self.assertFailure(response, 404)
        self.assertFalse(CallRoom.objects.exists())

    def test_a_removed_member_cannot_start_one(self):
        Participant.objects.filter(conversation=self.group, user=self.bob).update(
            status=Participant.Status.REMOVED
        )
        self.assertFailure(self.start(user=self.bob), 404)

    def test_a_pending_member_cannot_start_one(self):
        Participant.objects.filter(conversation=self.group, user=self.bob).update(
            status=Participant.Status.PENDING
        )
        self.assertFailure(self.start(user=self.bob), 404)

    def test_the_service_refuses_a_non_member_too(self):
        # The view is not the only door in.
        with self.assertRaises(Exception) as caught:
            services.start_call(self.dave, self.group, 'audio')
        self.assertEqual(caught.exception.status_code, 403)

    def test_only_admins_start_a_call_in_a_broadcast_group(self):
        self.group.mode = Conversation.Mode.BROADCAST
        self.group.save()

        self.assertFailure(self.start(user=self.bob), 403)
        self.assertSuccess(self.start(user=self.alice), 201)

    def test_one_live_call_per_conversation(self):
        first = self.start_ok()
        response = self.start(user=self.bob)
        body = self.assertFailure(response, 409)
        # Tells the client which call to join instead.
        self.assertEqual(body['data']['call_id'], first['call']['id'])
        self.assertEqual(CallRoom.objects.count(), 1)

    def test_a_new_call_can_start_once_the_last_one_ended(self):
        first = self.start_ok()
        self.post('end', first['call']['id'])
        self.assertSuccess(self.start(user=self.bob), 201)

    def test_an_unknown_call_type_is_refused(self):
        response = self.start(call_type='hologram')
        self.assertFailure(response, 400)

    def test_calling_is_refused_when_livekit_is_not_configured(self):
        with self.settings(LIVEKIT_API_SECRET=''):
            self.assertFailure(self.start(), 503)
        # And nothing was written for a call that could never be joined.
        self.assertFalse(CallRoom.objects.exists())

    def test_a_blocked_direct_call_is_refused(self):
        from user.blocks import block

        block(self.bob, user=self.alice)
        # Same answer either way round, so a block cannot be discovered.
        self.assertFailure(self.start(self.direct, user=self.alice), 403)
        self.assertFailure(self.start(self.direct, user=self.bob), 403)

    def test_starting_is_rate_limited(self):
        with mock.patch.object(
            services, 'start_call', side_effect=CallServiceError('x')
        ):
            statuses = set()
            for _ in range(70):
                statuses.add(self.start().status_code)
        self.assertIn(429, statuses)


# -- joining, and the plan's participant limit ---------------------------------


class JoinCallTests(CallTestBase):
    def setUp(self):
        super().setUp()
        self.call_id = self.start_ok()['call']['id']

    def test_a_member_joins_and_the_call_becomes_active(self):
        data = self.assertSuccess(self.join(self.call_id, self.bob))
        self.assertEqual(data['call']['status'], 'active')
        self.assertIsNotNone(data['call']['started_at'])
        self.assertEqual(data['call']['participant_count'], 2)

        claims = decode(data['livekit']['token'])
        self.assertEqual(claims['sub'], str(self.bob.pk))
        self.assertEqual(claims['video']['room'], CallRoom.objects.get().room_name)

    def test_a_non_member_cannot_join(self):
        self.assertFailure(self.join(self.call_id, self.dave), 404)
        self.assertEqual(CallParticipant.objects.filter(user=self.dave).count(), 0)

    def test_the_service_refuses_a_non_member_too(self):
        with self.assertRaises(Exception) as caught:
            services.join_call(self.dave, self.call_id)
        self.assertEqual(caught.exception.status_code, 403)

    def test_somebody_removed_mid_call_cannot_rejoin(self):
        self.assertSuccess(self.join(self.call_id, self.bob))
        Participant.objects.filter(conversation=self.group, user=self.bob).update(
            status=Participant.Status.REMOVED
        )
        self.assertFailure(self.join(self.call_id, self.bob), 404)

    def test_an_ended_call_cannot_be_joined(self):
        self.post('end', self.call_id, user=self.alice)
        self.assertFailure(self.join(self.call_id, self.bob), 409)

    def test_joining_twice_is_one_place_and_a_fresh_token(self):
        first = self.assertSuccess(self.join(self.call_id, self.bob))
        second = self.assertSuccess(self.join(self.call_id, self.bob))

        self.assertEqual(
            CallParticipant.objects.filter(user=self.bob, left_at__isnull=True).count(), 1
        )
        # Both work for the same person and room. (They may be byte-identical
        # when issued in the same second - a token carries no random part - so
        # what is checked is what each one lets them do, not that they differ.)
        self.assertEqual(
            decode(first['livekit']['token'])['video'],
            decode(second['livekit']['token'])['video'],
        )
        self.assertEqual(decode(second['livekit']['token'])['sub'], str(self.bob.pk))

    def test_a_participant_who_left_can_come_back(self):
        self.join(self.call_id, self.bob)
        self.post('leave', self.call_id, user=self.bob)
        self.assertSuccess(self.join(self.call_id, self.bob))

        stays = CallParticipant.objects.filter(user=self.bob)
        self.assertEqual(stays.count(), 2)  # the history keeps both
        self.assertEqual(stays.filter(left_at__isnull=True).count(), 1)

    # -- the limit ----------------------------------------------------------

    def extra_members(self, count):
        users = []
        for n in range(count):
            user = make_user(f'guest{n}')
            self.group.add_participant(user)
            users.append(user)
        return users

    def test_the_free_plan_caps_a_call_at_four(self):
        # alice is in; three more fit.
        guests = self.extra_members(5)
        for user in guests[:3]:
            self.assertSuccess(self.join(self.call_id, user))

        full = self.join(self.call_id, guests[3])
        body = self.assertFailure(full, 403)
        self.assertIn('Free', body['message'])
        self.assertIn('4', body['message'])
        self.assertEqual(CallParticipant.objects.filter(left_at__isnull=True).count(), 4)

    def test_somebody_already_in_the_call_is_not_turned_away_when_it_is_full(self):
        guests = self.extra_members(3)
        for user in guests:
            self.join(self.call_id, user)
        # Full - but a refresh must still get the person a token.
        self.assertSuccess(self.join(self.call_id, guests[0]))

    def test_leaving_gives_the_place_back(self):
        guests = self.extra_members(4)
        for user in guests[:3]:
            self.join(self.call_id, user)
        self.assertFailure(self.join(self.call_id, guests[3]), 403)

        self.post('leave', self.call_id, user=guests[0])
        self.assertSuccess(self.join(self.call_id, guests[3]))

    def test_the_limit_is_the_group_owners_plan_not_the_joiners(self):
        # A Premium member does not lift a Free owner's cap.
        guests = self.extra_members(4)
        put_on_plan(guests[3], 'business')
        for user in guests[:3]:
            self.join(self.call_id, user)
        self.assertFailure(self.join(self.call_id, guests[3]), 403)

    def test_upgrading_the_owner_raises_the_limit(self):
        guests = self.extra_members(4)
        for user in guests[:3]:
            self.join(self.call_id, user)
        put_on_plan(self.alice, 'premium')  # 25
        self.assertSuccess(self.join(self.call_id, guests[3]))

    def test_an_unlimited_plan_has_no_cap(self):
        put_on_plan(self.alice, 'enterprise')
        for user in self.extra_members(6):
            self.assertSuccess(self.join(self.call_id, user))

    def test_the_limit_is_enforced_by_the_service_not_the_client(self):
        """Nothing in the request can raise it - there is no field to send."""
        guests = self.extra_members(4)
        for user in guests[:3]:
            self.join(self.call_id, user)

        self.as_user(guests[3])
        response = self.client.post(
            self.url('join', self.call_id),
            {'max_participants': 100, 'plan': 'enterprise', 'limit': None},
            format='json',
        )
        self.assertFailure(response, 403)

    def test_a_token_nobody_used_gives_its_place_back(self):
        guests = self.extra_members(4)
        for user in guests[:3]:
            self.join(self.call_id, user)
        self.assertFailure(self.join(self.call_id, guests[3]), 403)

        # guest0 asked to join and never connected; their token is long dead.
        stay = CallParticipant.objects.get(user=guests[0])
        stay.token_expires_at = timezone.now() - timedelta(minutes=5)
        stay.save()
        # ...and alice, who did connect, is left alone.
        CallParticipant.objects.filter(user=self.alice).update(connected_at=timezone.now())

        services.sweep_stale_calls()

        stay.refresh_from_db()
        self.assertIsNotNone(stay.left_at)
        self.assertSuccess(self.join(self.call_id, guests[3]))


# -- leaving, declining, ending ------------------------------------------------


class LeaveAndEndTests(CallTestBase):
    def setUp(self):
        super().setUp()
        self.call_id = self.start_ok()['call']['id']
        self.join(self.call_id, self.bob)
        self.as_user(self.alice)

    def call(self):
        return CallRoom.objects.get(pk=self.call_id)

    def test_leaving_records_when_and_for_how_long(self):
        self.assertSuccess(self.post('leave', self.call_id, user=self.bob))
        stay = CallParticipant.objects.get(user=self.bob)
        self.assertIsNotNone(stay.left_at)
        self.assertIsNotNone(stay.duration)
        self.assertEqual(self.call().status, 'active')  # alice is still there

    def test_the_last_one_out_ends_the_call(self):
        self.post('leave', self.call_id, user=self.bob)
        self.post('leave', self.call_id, user=self.alice)

        call = self.call()
        self.assertEqual(call.status, 'ended')
        self.assertEqual(call.end_reason, 'empty')
        self.assertIsNotNone(call.ended_at)

    def test_hanging_up_before_anybody_answers_is_unanswered(self):
        other = self.start_ok(conversation=self.direct)['call']['id']
        self.post('leave', other, user=self.alice)
        self.assertEqual(CallRoom.objects.get(pk=other).end_reason, 'unanswered')

    def test_leaving_twice_is_not_an_error(self):
        self.post('leave', self.call_id, user=self.bob)
        self.assertSuccess(self.post('leave', self.call_id, user=self.bob))

    def test_the_person_who_started_it_can_end_it_for_everyone(self):
        self.assertSuccess(self.post('end', self.call_id, user=self.alice))
        call = self.call()
        self.assertEqual(call.status, 'ended')
        self.assertEqual(call.end_reason, 'ended')
        self.assertEqual(call.ended_by, self.alice)
        # Everybody was put out, with a duration each.
        self.assertFalse(call.participants.filter(left_at__isnull=True).exists())

    def test_a_group_admin_can_end_a_call_they_did_not_start(self):
        self.post('leave', self.call_id, user=self.alice)
        services.join_call(self.carol, self.call_id)
        Participant.objects.filter(conversation=self.group, user=self.carol).update(
            role=Participant.Role.ADMIN
        )
        # alice started it and has gone; carol is an admin.
        self.assertSuccess(self.post('end', self.call_id, user=self.carol))

    def test_an_ordinary_member_cannot_end_it(self):
        self.assertFailure(self.post('end', self.call_id, user=self.bob), 403)
        self.assertEqual(self.call().status, 'active')

    def test_a_non_member_cannot_end_it(self):
        self.assertFailure(self.post('end', self.call_id, user=self.dave), 404)

    def test_either_person_can_end_a_direct_call(self):
        direct = self.start_ok(conversation=self.direct)['call']['id']
        self.assertSuccess(self.post('end', direct, user=self.bob))

    def test_ending_closes_the_room_on_livekit(self):
        with mock.patch('calls.services.close_room') as closed:
            with self.captureOnCommitCallbacks(execute=True):
                self.post('end', self.call_id, user=self.alice)
        closed.assert_called_once_with(self.call().room_name)

    def test_ending_twice_is_not_an_error(self):
        self.post('end', self.call_id, user=self.alice)
        self.assertSuccess(self.post('end', self.call_id, user=self.alice))

    # -- declining -----------------------------------------------------------

    def test_declining_a_direct_call_ends_it(self):
        direct = self.start_ok(conversation=self.direct)['call']['id']
        self.assertSuccess(self.post('reject', direct, user=self.bob))

        call = CallRoom.objects.get(pk=direct)
        self.assertEqual(call.status, 'ended')
        self.assertEqual(call.end_reason, 'declined')
        self.assertTrue(
            call.events.filter(type=CallEvent.Type.REJECTED, user=self.bob).exists()
        )

    def test_declining_a_group_call_leaves_it_running(self):
        self.assertSuccess(self.post('reject', self.call_id, user=self.carol))
        self.assertEqual(self.call().status, 'active')
        self.assertTrue(
            self.call().events.filter(type=CallEvent.Type.REJECTED, user=self.carol).exists()
        )

    def test_you_cannot_decline_your_own_call(self):
        self.assertFailure(self.post('reject', self.call_id, user=self.alice), 400)

    def test_you_cannot_decline_a_call_you_are_in(self):
        self.assertFailure(self.post('reject', self.call_id, user=self.bob), 400)

    def test_a_non_member_cannot_decline(self):
        self.assertFailure(self.post('reject', self.call_id, user=self.dave), 404)


# -- reading -------------------------------------------------------------------


class ReadCallTests(CallTestBase):
    def test_a_member_reads_a_call(self):
        call_id = self.start_ok()['call']['id']
        self.as_user(self.bob)
        data = self.assertSuccess(self.client.get(self.url('detail', call_id)))
        self.assertEqual(data['id'], call_id)
        self.assertNotIn('token', json.dumps(data))
        self.assertNotIn('livekit', data)

    def test_a_non_member_cannot_read_it_or_learn_it_exists(self):
        call_id = self.start_ok()['call']['id']
        self.as_user(self.dave)
        self.assertFailure(self.client.get(self.url('detail', call_id)), 404)

    def test_history_is_listed_per_conversation(self):
        first = self.start_ok()['call']['id']
        self.post('end', first)
        second = self.start_ok()['call']['id']
        other = self.start_ok(conversation=self.direct)['call']['id']

        data = self.assertSuccess(
            self.client.get(reverse('calls:call-list'), {'conversation': self.group.pk})
        )
        self.assertEqual([c['id'] for c in data['results']], [second, first])
        self.assertNotIn(other, [c['id'] for c in data['results']])

    def test_the_list_only_holds_my_conversations(self):
        self.start_ok()
        self.as_user(self.dave)
        data = self.assertSuccess(self.client.get(reverse('calls:call-list')))
        self.assertEqual(data['results'], [])

    def test_the_list_can_ask_for_what_is_live(self):
        ended = self.start_ok()['call']['id']
        self.post('end', ended)
        live = self.start_ok()['call']['id']
        data = self.assertSuccess(
            self.client.get(reverse('calls:call-list'), {'status': 'ringing'})
        )
        self.assertEqual([c['id'] for c in data['results']], [live])


# -- recording -----------------------------------------------------------------


@override_settings(**LIVEKIT)
class RecordingTests(CallTestBase):
    def setUp(self):
        super().setUp()
        # A plan that records; the group is alice's.
        put_on_plan(self.alice, 'premium')
        self.call_id = self.start_ok()['call']['id']
        self.join(self.call_id, self.bob)
        self.join(self.call_id, self.carol)
        self.as_user(self.alice)

    def start_recording(self, user=None):
        return self.post('recording-start', self.call_id, user=user)

    def stop_recording(self, user=None):
        return self.post('recording-stop', self.call_id, user=user)

    def recording_started(self, user=None, egress_id='EG_1'):
        with mock.patch(
            'calls.livekit.start_recording',
            return_value=egress(lk.EgressStatus.EGRESS_ACTIVE, egress_id=egress_id),
        ) as started:
            response = self.start_recording(user)
        return response, started

    def finish(self, recording, egress_id='EG_1', size=4096, seconds=90):
        services.apply_egress_info(
            egress(
                lk.EgressStatus.EGRESS_COMPLETE,
                egress_id=egress_id,
                file_results=[
                    lk.FileInfo(
                        filename=recording.s3_key, size=size, duration=seconds * 10**9
                    )
                ],
            )
        )
        recording.refresh_from_db()
        return recording

    def test_the_call_starter_starts_a_recording(self):
        response, started = self.recording_started()
        data = self.assertSuccess(response, 201)

        self.assertEqual(data['status'], 'recording')
        recording = CallRecording.objects.get()
        self.assertEqual(recording.started_by, self.alice)
        self.assertEqual(recording.egress_id, 'EG_1')

        kwargs = started.call_args.kwargs
        self.assertEqual(kwargs['room_name'], CallRoom.objects.get().room_name)
        # Under its own prefix, in this conversation and call.
        self.assertTrue(
            kwargs['key'].startswith(f'call-recordings/{self.group.pk}/{self.call_id}/')
        )
        # Never the bucket or the key in the API.
        self.assertNotIn('s3_key', data)
        self.assertNotIn(recording.s3_key, json.dumps(data))

    def test_an_audio_call_is_recorded_as_audio(self):
        _, started = self.recording_started()
        self.assertTrue(started.call_args.kwargs['audio_only'])
        self.assertTrue(started.call_args.kwargs['key'].endswith('.ogg'))

    def test_the_plan_must_include_recording(self):
        put_on_plan(self.alice, 'free')
        with mock.patch('calls.livekit.start_recording') as started:
            body = self.assertFailure(self.start_recording(self.alice), 403)
        self.assertIn('does not include call recording', body['message'])
        started.assert_not_called()
        self.assertFalse(CallRecording.objects.exists())

    def test_the_plan_is_the_group_owners_not_the_requesters(self):
        # bob is on Business, but the group is alice's - and alice is on Free.
        put_on_plan(self.alice, 'free')
        put_on_plan(self.bob, 'business')
        Participant.objects.filter(conversation=self.group, user=self.bob).update(
            role=Participant.Role.ADMIN
        )
        with mock.patch('calls.livekit.start_recording') as started:
            self.assertFailure(self.start_recording(self.bob), 403)
        started.assert_not_called()

    def test_an_ordinary_member_cannot_record(self):
        with mock.patch('calls.livekit.start_recording') as started:
            self.assertFailure(self.start_recording(self.bob), 403)
        started.assert_not_called()

    def test_a_non_member_cannot_record(self):
        self.assertFailure(self.start_recording(self.dave), 404)

    def test_a_group_admin_can_record_a_call_they_did_not_start(self):
        Participant.objects.filter(conversation=self.group, user=self.bob).update(
            role=Participant.Role.ADMIN
        )
        response, _ = self.recording_started(user=self.bob)
        self.assertSuccess(response, 201)

    def test_only_one_recording_at_a_time(self):
        self.recording_started()
        with mock.patch('calls.livekit.start_recording') as started:
            self.assertFailure(self.start_recording(self.alice), 409)
        started.assert_not_called()

    def test_an_ended_call_cannot_be_recorded(self):
        self.post('end', self.call_id, user=self.alice)
        self.assertFailure(self.start_recording(self.alice), 409)

    def test_a_livekit_failure_is_a_503_and_leaves_nothing_in_flight(self):
        with mock.patch(
            'calls.livekit.start_recording', side_effect=CallServiceError('down')
        ):
            self.assertFailure(self.start_recording(self.alice), 503)

        self.assertEqual(CallRecording.objects.get().status, 'failed')
        # ...so trying again is not refused as "already recording".
        response, _ = self.recording_started()
        self.assertSuccess(response, 201)

    def test_recording_is_refused_where_no_bucket_is_configured(self):
        with self.settings(LIVEKIT_RECORDING_BUCKET=''):
            self.assertFailure(self.start_recording(self.alice), 503)

    def test_stopping(self):
        self.recording_started()
        with mock.patch(
            'calls.livekit.stop_recording',
            return_value=egress(lk.EgressStatus.EGRESS_ENDING),
        ) as stopped:
            data = self.assertSuccess(self.stop_recording(self.alice))
        stopped.assert_called_once_with('EG_1')
        self.assertEqual(data['status'], 'stopping')

    def test_stopping_needs_the_same_standing_as_starting(self):
        self.recording_started()
        with mock.patch('calls.livekit.stop_recording') as stopped:
            self.assertFailure(self.stop_recording(self.bob), 403)
        stopped.assert_not_called()

    def test_stopping_when_nothing_is_recording(self):
        self.assertFailure(self.stop_recording(self.alice), 400)

    def test_a_failed_stop_puts_it_back_to_recording(self):
        self.recording_started()
        with mock.patch(
            'calls.livekit.stop_recording', side_effect=CallServiceError('down')
        ):
            self.assertFailure(self.stop_recording(self.alice), 503)
        self.assertEqual(CallRecording.objects.get().status, 'recording')

    def test_the_file_finishing_completes_it_and_sets_the_retention(self):
        self.recording_started()
        recording = self.finish(CallRecording.objects.get())

        self.assertEqual(recording.status, 'completed')
        self.assertEqual(recording.size_bytes, 4096)
        self.assertEqual(recording.duration_seconds, 90)
        # Premium keeps recordings for 30 days.
        days = (recording.expires_at - recording.ended_at).days
        self.assertEqual(days, 30)

    def test_a_plan_with_no_retention_never_expires(self):
        put_on_plan(self.alice, 'enterprise')
        self.recording_started()
        recording = self.finish(CallRecording.objects.get())
        self.assertIsNone(recording.expires_at)

    def test_livekit_reporting_failure_marks_it_failed(self):
        self.recording_started()
        services.apply_egress_info(
            egress(lk.EgressStatus.EGRESS_FAILED, error='encoder crashed')
        )
        recording = CallRecording.objects.get()
        self.assertEqual(recording.status, 'failed')
        # And the room can be recorded again.
        response, _ = self.recording_started(egress_id='EG_2')
        self.assertSuccess(response, 201)

    def test_a_late_update_never_moves_a_finished_recording(self):
        self.recording_started()
        recording = self.finish(CallRecording.objects.get())
        services.apply_egress_info(egress(lk.EgressStatus.EGRESS_ACTIVE))
        recording.refresh_from_db()
        self.assertEqual(recording.status, 'completed')

    def test_ending_the_call_lets_the_recording_finish_by_itself(self):
        self.recording_started()
        self.post('end', self.call_id, user=self.alice)
        # Not touched by ending the call: LiveKit finishes it when the room
        # closes, and says so by webhook.
        self.assertEqual(CallRecording.objects.get().status, 'recording')
        self.finish(CallRecording.objects.get())
        self.assertEqual(CallRecording.objects.get().status, 'completed')

    def test_the_sweep_asks_livekit_about_a_recording_whose_webhook_never_came(self):
        self.recording_started()
        self.post('end', self.call_id, user=self.alice)
        CallRoom.objects.update(ended_at=timezone.now() - timedelta(minutes=30))

        done = egress(
            lk.EgressStatus.EGRESS_COMPLETE,
            file_results=[lk.FileInfo(size=10, duration=5 * 10**9)],
        )
        with mock.patch('calls.livekit.get_recording', return_value=done):
            services.sweep_stale_calls()
        self.assertEqual(CallRecording.objects.get().status, 'completed')

    def test_the_sweep_fails_a_recording_livekit_has_forgotten(self):
        self.recording_started()
        self.post('end', self.call_id, user=self.alice)
        CallRoom.objects.update(ended_at=timezone.now() - timedelta(minutes=30))

        with mock.patch('calls.livekit.get_recording', return_value=None):
            services.sweep_stale_calls()
        self.assertEqual(CallRecording.objects.get().status, 'failed')

    # -- viewing -------------------------------------------------------------

    def completed(self):
        self.recording_started()
        return self.finish(CallRecording.objects.get())

    def recording_url(self, recording):
        return reverse('calls:recording-detail', args=[recording.pk])

    def test_a_participant_lists_and_downloads_a_recording(self):
        recording = self.completed()
        self.as_user(self.bob)

        listed = self.assertSuccess(
            self.client.get(self.url('recordings', self.call_id))
        )
        self.assertEqual([r['id'] for r in listed], [recording.pk])
        # The list carries no link; the detail does.
        self.assertNotIn('download_url', listed[0])

        with mock.patch(
            'calls.storage.download_url', return_value='https://signed.example/file'
        ) as signed:
            data = self.assertSuccess(self.client.get(self.recording_url(recording)))
        self.assertEqual(data['download_url'], 'https://signed.example/file')
        signed.assert_called_once()

    def test_a_member_who_was_not_on_the_call_cannot_see_its_recording(self):
        recording = self.completed()
        self.group.add_participant(self.dave)
        self.as_user(self.dave)

        self.assertFailure(self.client.get(self.url('recordings', self.call_id)), 404)
        self.assertFailure(self.client.get(self.recording_url(recording)), 404)

    def test_a_stranger_cannot_see_it(self):
        recording = self.completed()
        self.as_user(self.dave)
        self.assertFailure(self.client.get(self.recording_url(recording)), 404)

    def test_somebody_removed_from_the_group_loses_access(self):
        recording = self.completed()
        Participant.objects.filter(conversation=self.group, user=self.bob).update(
            status=Participant.Status.REMOVED
        )
        self.as_user(self.bob)
        self.assertFailure(self.client.get(self.recording_url(recording)), 404)

    def test_there_is_no_link_until_it_is_ready(self):
        self.recording_started()
        recording = CallRecording.objects.get()
        with mock.patch('calls.storage.download_url') as signed:
            data = self.assertSuccess(self.client.get(self.recording_url(recording)))
        self.assertIsNone(data['download_url'])
        signed.assert_not_called()

    # -- deleting ------------------------------------------------------------

    def test_the_owner_deletes_a_recording(self):
        recording = self.completed()
        with mock.patch('calls.storage.delete_file') as removed:
            response = self.client.delete(self.recording_url(recording))
        self.assertEqual(response.status_code, 204)
        removed.assert_called_once()

        recording.refresh_from_db()
        self.assertEqual(recording.status, 'deleted')
        self.assertEqual(recording.deleted_by, self.alice)
        self.assertIsNotNone(recording.deleted_at)
        # The row stays, the pointer to the file does not.
        self.assertEqual(recording.s3_key, '')

    def test_an_ordinary_participant_cannot_delete_it(self):
        recording = self.completed()
        self.as_user(self.bob)
        with mock.patch('calls.storage.delete_file') as removed:
            self.assertFailure(self.client.delete(self.recording_url(recording)), 403)
        removed.assert_not_called()
        recording.refresh_from_db()
        self.assertEqual(recording.status, 'completed')

    def test_a_stranger_cannot_delete_it(self):
        recording = self.completed()
        self.as_user(self.dave)
        self.assertFailure(self.client.delete(self.recording_url(recording)), 404)

    def test_a_recording_in_progress_cannot_be_deleted(self):
        self.recording_started()
        recording = CallRecording.objects.get()
        self.assertFailure(self.client.delete(self.recording_url(recording)), 409)

    def test_the_row_is_not_marked_deleted_if_the_file_would_not_go(self):
        recording = self.completed()
        with mock.patch(
            'calls.storage.delete_file', side_effect=CallServiceError('s3 down')
        ):
            self.assertFailure(self.client.delete(self.recording_url(recording)), 503)
        recording.refresh_from_db()
        self.assertEqual(recording.status, 'completed')
        self.assertNotEqual(recording.s3_key, '')

    def test_a_deleted_recording_has_no_link(self):
        recording = self.completed()
        with mock.patch('calls.storage.delete_file'):
            self.client.delete(self.recording_url(recording))
        data = self.assertSuccess(self.client.get(self.recording_url(recording)))
        self.assertEqual(data['status'], 'deleted')
        self.assertIsNone(data['download_url'])

    # -- retention -----------------------------------------------------------

    def test_retention_deletes_what_has_expired_and_nothing_else(self):
        expired = self.completed()
        CallRecording.objects.filter(pk=expired.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        # A second, still inside its window.
        kept = CallRecording.objects.create(
            call_room=CallRoom.objects.get(),
            status=CallRecording.Status.COMPLETED,
            s3_bucket='recordings-bucket',
            s3_key='call-recordings/x/y/kept.mp4',
            expires_at=timezone.now() + timedelta(days=5),
        )

        with mock.patch('calls.storage.delete_file') as removed:
            count = services.purge_expired_recordings()

        self.assertEqual(count, 1)
        self.assertEqual(removed.call_count, 1)
        expired.refresh_from_db()
        kept.refresh_from_db()
        self.assertEqual(expired.status, 'deleted')
        self.assertIsNone(expired.deleted_by)  # retention, not a person
        self.assertEqual(kept.status, 'completed')

    def test_one_bad_file_does_not_stop_the_purge(self):
        first = self.completed()
        second = CallRecording.objects.create(
            call_room=CallRoom.objects.get(),
            status=CallRecording.Status.COMPLETED,
            s3_bucket='recordings-bucket',
            s3_key='call-recordings/x/y/second.mp4',
        )
        CallRecording.objects.update(expires_at=timezone.now() - timedelta(days=1))

        with mock.patch(
            'calls.storage.delete_file',
            side_effect=[CallServiceError('boom'), None],
        ):
            count = services.purge_expired_recordings()

        self.assertEqual(count, 1)
        statuses = set(CallRecording.objects.values_list('status', flat=True))
        self.assertEqual(statuses, {'completed', 'deleted'})


# -- LiveKit's webhook ---------------------------------------------------------


@override_settings(**LIVEKIT)
class WebhookTests(CallTestBase):
    def setUp(self):
        super().setUp()
        self.call_id = self.start_ok()['call']['id']
        self.join(self.call_id, self.bob)
        self.room = CallRoom.objects.get().room_name
        self.as_user(self.alice)
        # LiveKit is not signed in as anybody.
        self.livekit = APIClient()

    def deliver(self, body, signed_with=API_SECRET, tamper=False):
        raw = json.dumps(body)
        digest = base64.b64encode(hashlib.sha256(raw.encode()).digest()).decode()
        token = lk.AccessToken(API_KEY, signed_with).with_sha256(digest).to_jwt()
        if tamper:
            raw = raw.replace('participant_left', 'participant_lft')
        return self.livekit.generic(
            'POST',
            reverse('calls:livekit-webhook'),
            raw,
            content_type='application/webhook+json',
            HTTP_AUTHORIZATION=token,
        )

    def left(self, user, **participant):
        return {
            'event': 'participant_left',
            'room': {'name': self.room},
            'participant': {'identity': str(user.pk), **participant},
        }

    def test_an_unsigned_request_is_refused(self):
        response = self.livekit.generic(
            'POST',
            reverse('calls:livekit-webhook'),
            json.dumps(self.left(self.bob)),
            content_type='application/webhook+json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(CallParticipant.objects.get(user=self.bob).left_at)

    def test_a_forged_signature_is_refused(self):
        response = self.deliver(self.left(self.bob), signed_with='not-the-secret' * 4)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(CallParticipant.objects.get(user=self.bob).left_at)

    def test_a_body_changed_after_signing_is_refused(self):
        response = self.deliver(self.left(self.bob), tamper=True)
        self.assertEqual(response.status_code, 400)

    def test_somebody_dropping_out_frees_their_place(self):
        self.assertEqual(self.deliver(self.left(self.bob)).status_code, 200)
        self.assertIsNotNone(CallParticipant.objects.get(user=self.bob).left_at)
        self.assertEqual(CallRoom.objects.get().status, 'active')

    def test_the_last_one_dropping_out_ends_the_call(self):
        self.deliver(self.left(self.bob))
        self.deliver(self.left(self.alice))
        self.assertEqual(CallRoom.objects.get().status, 'ended')

    def test_a_reconnect_is_not_a_departure(self):
        """A refresh makes LiveKit drop the old session for a duplicate identity;
        the person is still there on the new one."""
        response = self.deliver(
            self.left(self.bob, disconnect_reason='DUPLICATE_IDENTITY')
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(CallParticipant.objects.get(user=self.bob).left_at)

    def test_arriving_marks_them_connected(self):
        self.deliver(
            {
                'event': 'participant_joined',
                'room': {'name': self.room},
                'participant': {'identity': str(self.bob.pk)},
            }
        )
        self.assertIsNotNone(CallParticipant.objects.get(user=self.bob).connected_at)

    def test_the_recorder_is_not_a_person(self):
        self.deliver(
            {
                'event': 'participant_joined',
                'room': {'name': self.room},
                'participant': {'identity': 'EG_abc123'},
            }
        )
        self.assertEqual(CallParticipant.objects.count(), 2)

    def test_a_closed_room_ends_the_call(self):
        self.deliver({'event': 'room_finished', 'room': {'name': self.room}})
        self.assertEqual(CallRoom.objects.get().status, 'ended')

    def test_an_unknown_room_is_ignored(self):
        response = self.deliver(
            {'event': 'room_finished', 'room': {'name': 'call-somebody-elses'}}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CallRoom.objects.get().status, 'active')

    def test_egress_events_update_the_recording(self):
        put_on_plan(self.alice, 'premium')
        with mock.patch(
            'calls.livekit.start_recording',
            return_value=egress(lk.EgressStatus.EGRESS_ACTIVE),
        ):
            self.post('recording-start', self.call_id, user=self.alice)

        self.deliver(
            {
                'event': 'egress_ended',
                'egressInfo': {
                    'egressId': 'EG_1',
                    'status': 'EGRESS_COMPLETE',
                    'fileResults': [{'size': '2048', 'duration': str(30 * 10**9)}],
                },
            }
        )
        recording = CallRecording.objects.get()
        self.assertEqual(recording.status, 'completed')
        self.assertEqual(recording.size_bytes, 2048)
        self.assertEqual(recording.duration_seconds, 30)


# -- the sweep -----------------------------------------------------------------


class SweepTests(CallTestBase):
    def test_a_direct_call_nobody_answers_is_given_up(self):
        call_id = self.start_ok(conversation=self.direct)['call']['id']
        CallRoom.objects.update(created_at=timezone.now() - timedelta(minutes=5))

        self.assertEqual(services.sweep_stale_calls(), 1)
        call = CallRoom.objects.get(pk=call_id)
        self.assertEqual(call.status, 'ended')
        self.assertEqual(call.end_reason, 'unanswered')

    def test_a_call_still_ringing_is_left_alone(self):
        call_id = self.start_ok(conversation=self.direct)['call']['id']
        self.assertEqual(services.sweep_stale_calls(), 0)
        self.assertEqual(CallRoom.objects.get(pk=call_id).status, 'ringing')

    def test_an_answered_call_is_not_given_up(self):
        call_id = self.start_ok(conversation=self.direct)['call']['id']
        self.join(call_id, self.bob)
        CallRoom.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(services.sweep_stale_calls(), 0)

    def test_a_group_waiting_for_people_is_not_given_up(self):
        self.start_ok()
        CallRoom.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        CallParticipant.objects.update(connected_at=timezone.now())
        self.assertEqual(services.sweep_stale_calls(), 0)

    def test_a_call_that_nobody_ended_is_ended(self):
        self.start_ok()
        CallRoom.objects.update(created_at=timezone.now() - timedelta(hours=30))
        CallParticipant.objects.update(connected_at=timezone.now())

        self.assertEqual(services.sweep_stale_calls(), 1)
        call = CallRoom.objects.get()
        self.assertEqual(call.end_reason, 'timed_out')


# -- the socket ----------------------------------------------------------------


class BroadcastTests(CallTestBase):
    def frames(self, action):
        # close_room too: ending a call closes its LiveKit room after commit,
        # and this test has no LiveKit to ask.
        with mock.patch('calls.services.send_to_group') as sent, mock.patch(
            'calls.services.close_room'
        ):
            with self.captureOnCommitCallbacks(execute=True):
                action()
        return [call.args[1] for call in sent.call_args_list]

    def test_starting_a_call_rings_the_conversation(self):
        frames = self.frames(self.start)

        self.assertEqual([f['type'] for f in frames], ['call.incoming'])
        frame = frames[0]
        self.assertEqual(frame['conversation_id'], self.group.pk)
        self.assertEqual(frame['user_id'], self.alice.pk)
        self.assertEqual(frame['call']['status'], 'ringing')

    def test_a_frame_never_carries_a_token(self):
        frames = self.frames(self.start)
        payload = json.dumps(frames, default=str)
        self.assertNotIn('"token"', payload)
        self.assertNotIn(API_SECRET, payload)
        # Nor the recordings' whereabouts.
        self.assertNotIn('s3_key', payload)

    def test_it_goes_to_the_conversations_own_group(self):
        from chat.services import conversation_group

        with mock.patch('calls.services.send_to_group') as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.start()
        self.assertEqual(sent.call_args.args[0], conversation_group(self.group.pk))

    def test_answering_says_who(self):
        call_id = self.start_ok()['call']['id']
        frames = self.frames(lambda: self.join(call_id, self.bob))
        self.assertEqual([f['type'] for f in frames], ['call.accepted'])
        self.assertEqual(frames[0]['user_id'], self.bob.pk)
        self.assertEqual(frames[0]['call']['status'], 'active')

    def test_refreshing_does_not_announce_an_arrival_again(self):
        call_id = self.start_ok()['call']['id']
        self.join(call_id, self.bob)
        self.assertEqual(self.frames(lambda: self.join(call_id, self.bob)), [])

    def test_declining(self):
        call_id = self.start_ok()['call']['id']
        frames = self.frames(lambda: self.post('reject', call_id, user=self.bob))
        self.assertEqual([f['type'] for f in frames], ['call.rejected'])
        self.assertEqual(frames[0]['user_id'], self.bob.pk)

    def test_ending(self):
        call_id = self.start_ok()['call']['id']
        frames = self.frames(lambda: self.post('end', call_id, user=self.alice))
        self.assertEqual([f['type'] for f in frames], ['call.ended'])
        self.assertEqual(frames[0]['call']['status'], 'ended')

    def test_a_refused_start_says_nothing(self):
        self.start_ok()
        frames = self.frames(lambda: self.start(user=self.bob))
        self.assertEqual(frames, [])

    def test_recording_is_announced_to_everyone_in_the_call(self):
        put_on_plan(self.alice, 'premium')
        call_id = self.start_ok()['call']['id']
        with mock.patch(
            'calls.livekit.start_recording',
            return_value=egress(lk.EgressStatus.EGRESS_ACTIVE),
        ):
            started = self.frames(lambda: self.post('recording-start', call_id))
        self.assertEqual([f['type'] for f in started], ['call.recording.started'])
        self.assertTrue(started[0]['call']['is_recording'])

        with mock.patch(
            'calls.livekit.stop_recording',
            return_value=egress(lk.EgressStatus.EGRESS_ENDING),
        ):
            stopped = self.frames(lambda: self.post('recording-stop', call_id))
        self.assertEqual([f['type'] for f in stopped], ['call.recording.stopped'])
        self.assertFalse(stopped[0]['call']['is_recording'])


@override_settings(**LIVEKIT)
class CallWebsocketTests(TransactionTestCase):
    """The frames reach a real socket.

    Channels routes a group event by turning its `type` into a method name, so a
    frame the consumer has no method for is dropped in silence - the call is
    recorded, the REST API works, and nobody's phone ever rings. TransactionTestCase
    because the broadcast happens on commit and the consumer reads the database
    from another thread.
    """

    HEADERS = [(b'origin', b'http://localhost:3000')]

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.tokens = {
            user.username: str(RefreshToken.for_user(user).access_token)
            for user in (self.alice, self.bob)
        }

    def connect_as(self, user):
        from loopd.asgi import application

        return WebsocketCommunicator(
            application,
            f'/ws/chat/?token={self.tokens[user.username]}',
            headers=self.HEADERS,
        )

    async def test_the_other_person_is_rung_and_the_caller_is_not(self):
        caller = self.connect_as(self.alice)
        await caller.connect()
        callee = self.connect_as(self.bob)
        await callee.connect()
        # alice hears bob arrive.
        presence = await caller.receive_json_from(timeout=3)
        self.assertEqual(presence['type'], 'presence')

        call, _ = await database_sync_to_async(services.start_call)(
            self.alice, self.conversation, 'video'
        )

        ring = await callee.receive_json_from(timeout=3)
        self.assertEqual(ring['type'], 'call.incoming')
        self.assertEqual(ring['call_id'], call.pk)
        self.assertEqual(ring['user_id'], self.alice.pk)
        self.assertEqual(ring['call']['call_type'], 'video')

        # The caller's own devices do not ring. Then the next thing they hear
        # is the call ending - proving the ring was skipped, not merely late.
        with mock.patch('calls.services.close_room'):
            await database_sync_to_async(services.end_call)(self.alice, call.pk)

        ended_for_caller = await caller.receive_json_from(timeout=3)
        self.assertEqual(ended_for_caller['type'], 'call.ended')
        ended_for_callee = await callee.receive_json_from(timeout=3)
        self.assertEqual(ended_for_callee['type'], 'call.ended')

        await caller.disconnect()
        await callee.disconnect()

    async def test_answering_reaches_the_socket(self):
        caller = self.connect_as(self.alice)
        await caller.connect()

        call, _ = await database_sync_to_async(services.start_call)(
            self.alice, self.conversation, 'audio'
        )
        await database_sync_to_async(services.join_call)(self.bob, call.pk)

        accepted = await caller.receive_json_from(timeout=3)
        self.assertEqual(accepted['type'], 'call.accepted')
        self.assertEqual(accepted['user_id'], self.bob.pk)

        await caller.disconnect()
