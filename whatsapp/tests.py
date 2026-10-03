"""Tests for WhatsApp: connecting a number, attaching it to groups, inviting,
the fan-out, and Meta's webhook.

Meta is never contacted. `whatsapp.graph._request` is the single seam, and a
fake answers it by URL. Webhook bodies are signed for real with the test app
secret, so the signature check is exercised rather than bypassed.
"""
import hashlib
import hmac
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from chat.models import ChannelConsent, Conversation, Message, Participant
from chat import membership
from chat.services import create_message
from user.tests import EnvelopeAssertions
from whatsapp import crypto, outbound
from whatsapp.models import (
    GroupChannel,
    WhatsAppConnection,
    WhatsAppContact,
    WhatsAppDelivery,
    WhatsAppWebhookEvent,
)

APP_SECRET = 'test-app-secret'
META = dict(
    WHATSAPP_APP_ID='123',
    WHATSAPP_APP_SECRET=APP_SECRET,
    WHATSAPP_WEBHOOK_VERIFY_TOKEN='verify-me',
    WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID='cfg-1',
    WHATSAPP_DEFAULT_TEMPLATE_NAME='',
    WHATSAPP_INVITE_TEMPLATE_NAME='',
)
WHATSAPP = ChannelConsent.Channel.WHATSAPP


class FakeGraph:
    """Answers Graph API calls by URL, and remembers what was asked."""

    def __init__(self, *, granted=('1001',), numbers=('2002',), send_error=None):
        self.granted = list(granted)
        self.numbers = list(numbers)
        self.send_error = send_error
        self.calls = []
        self.sent = []

    def __call__(self, method, url, *, token=None, payload=None, failure_message):
        self.calls.append((method, url, payload))
        path = url.split('graph.facebook.com/')[1].split('?')[0].split('/', 1)[1]
        if path == 'oauth/access_token':
            return {'access_token': 'BUSINESS-TOKEN'}
        if path == 'debug_token':
            return {'data': {'is_valid': True, 'granular_scopes': [
                {'scope': 'whatsapp_business_management', 'target_ids': self.granted},
            ]}}
        if path.endswith('/phone_numbers'):
            if getattr(self, 'numbers_error', None) is not None:
                raise self.numbers_error
            return {'data': [{'id': n} for n in self.numbers]}
        if path.endswith('/subscribed_apps') or path.endswith('/register'):
            return {'success': True}
        if path.endswith('/messages'):
            if self.send_error is not None:
                raise self.send_error
            self.sent.append(payload)
            return {'messages': [{'id': f'wamid.{len(self.sent)}'}]}
        if path.isdigit():
            return {'display_phone_number': '+1 555 000 1111', 'verified_name': 'Coach F',
                    'quality_rating': 'GREEN'}
        raise AssertionError(f'unexpected Graph call {method} {url}')


def make_user(name, phone=None, verified=True):
    user = User.objects.create_user(username=name, email=f'{name}@example.com')
    if phone:
        user.profile.phone_number = phone
        user.profile.phone_no_verified = verified
        user.profile.save()
    return user


def make_connection(owner, phone_number_id='2002', **kwargs):
    connection = WhatsAppConnection(
        owner=owner, waba_id='1001', phone_number_id=phone_number_id,
        display_phone_number='+1 555 000 1111', **kwargs,
    )
    connection.set_access_token('BUSINESS-TOKEN')
    connection.save()
    return connection


def opt_in(participant):
    membership.set_channel_consent(participant, WHATSAPP, ChannelConsent.Status.OPTED_IN)


@override_settings(**META)
class WhatsAppTestBase(EnvelopeAssertions, APITestCase):
    """F owns "Project F" and a WhatsApp number; B is a member with a verified phone."""

    def setUp(self):
        cache.clear()
        self.owner = make_user('fiona', phone='+15550000001')
        self.bob = make_user('bob', phone='+15550000002')
        self.carol = make_user('carol', phone='+15550000003')
        self.group = Conversation.start_group(self.owner, name='Project F')
        self.group.add_participant(self.bob)
        self.group.add_participant(self.carol)
        self.bob_row = Participant.objects.get(conversation=self.group, user=self.bob)
        self.carol_row = Participant.objects.get(conversation=self.group, user=self.carol)

        self.graph = FakeGraph()
        patcher = mock.patch('whatsapp.graph._request', side_effect=self.graph)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_authenticate(self.owner)

    def connect(self, **overrides):
        body = {'code': 'CODE', 'waba_id': '1001', 'phone_number_id': '2002', **overrides}
        return self.client.post(reverse('whatsapp:connection-list'), body, format='json')

    def attach(self, connection, group=None):
        channel = GroupChannel.objects.create(
            conversation=group or self.group, whatsapp_connection=connection,
            keyword=GroupChannel.unused_keyword(connection, (group or self.group).name),
        )
        return channel


# --- connecting ----------------------------------------------------------------


class ConnectTests(WhatsAppTestBase):
    def test_nothing_is_connected_until_the_owner_asks(self):
        data = self.assertSuccess(self.client.get(reverse('whatsapp:connection-list')))
        self.assertEqual(data['results'], [])

    def test_config_exposes_public_signup_ids_only(self):
        data = self.assertSuccess(self.client.get(reverse('whatsapp:config')))
        self.assertEqual(data, {'enabled': True, 'app_id': '123', 'config_id': 'cfg-1',
                                'graph_version': 'v23.0'})

    def test_embedded_signup_connects_the_number(self):
        data = self.assertSuccess(self.connect(), 201)

        connection = WhatsAppConnection.objects.get()
        self.assertEqual(connection.owner, self.owner)
        self.assertEqual(connection.status, WhatsAppConnection.Status.ACTIVE)
        self.assertEqual(data['verified_name'], 'Coach F')
        # Subscribed to webhooks and registered for Cloud API.
        urls = [url for _, url, _ in self.graph.calls]
        self.assertTrue(any(u.endswith('1001/subscribed_apps') for u in urls))
        self.assertTrue(any(u.endswith('2002/register') for u in urls))

    def test_the_token_is_encrypted_and_never_returned(self):
        response = self.connect()
        self.assertNotIn('BUSINESS-TOKEN', response.content.decode())

        connection = WhatsAppConnection.objects.get()
        self.assertNotIn('BUSINESS-TOKEN', connection.access_token_encrypted)
        self.assertEqual(connection.access_token, 'BUSINESS-TOKEN')

    def test_a_waba_the_token_does_not_cover_is_refused(self):
        self.graph.granted = ['9999']
        body = self.assertFailure(self.connect(), 400)
        self.assertEqual(body['data']['reason'], 'waba_not_granted')
        self.assertFalse(WhatsAppConnection.objects.exists())

    def test_a_grant_not_limited_to_assets_is_accepted(self):
        # Meta omits target_ids for such a grant; reading the WABA with the
        # token is then the proof.
        self.graph.granted = []
        self.assertSuccess(self.connect(), 201)

    def test_an_unlisted_grant_still_cannot_reach_a_waba_meta_refuses(self):
        from whatsapp.graph import WhatsAppError

        self.graph.granted = []
        self.graph.numbers_error = WhatsAppError('no access', code=100)
        self.assertFailure(self.connect(), 503)
        self.assertFalse(WhatsAppConnection.objects.exists())

    def test_a_number_outside_the_waba_is_refused(self):
        self.graph.numbers = ['2999']
        body = self.assertFailure(self.connect(), 400)
        self.assertEqual(body['data']['reason'], 'number_not_in_waba')

    def test_a_number_connected_by_somebody_else_is_refused(self):
        make_connection(self.bob)
        body = self.assertFailure(self.connect(), 400)
        self.assertEqual(body['data']['reason'], 'number_in_use')

    def test_reconnecting_your_own_number_updates_it(self):
        self.assertSuccess(self.connect(), 201)
        self.assertSuccess(self.connect(), 201)
        self.assertEqual(WhatsAppConnection.objects.count(), 1)

    def test_somebody_elses_connection_is_a_404(self):
        connection = make_connection(self.bob)
        response = self.client.get(reverse('whatsapp:connection-detail', args=[connection.pk]))
        self.assertFailure(response, 404)

    def test_disconnecting_detaches_every_group(self):
        connection = make_connection(self.owner)
        self.attach(connection)
        self.assertSuccess(
            self.client.delete(reverse('whatsapp:connection-detail', args=[connection.pk]))
        )
        connection.refresh_from_db()
        self.assertEqual(connection.status, WhatsAppConnection.Status.DISCONNECTED)
        self.assertEqual(connection.access_token_encrypted, '')
        self.assertFalse(GroupChannel.objects.exists())


# --- attaching to a group ---------------------------------------------------------


class AttachTests(WhatsAppTestBase):
    def url(self, group=None):
        return reverse('whatsapp:group', args=[(group or self.group).pk])

    def test_admin_attaches_their_own_number(self):
        connection = make_connection(self.owner)
        data = self.assertSuccess(
            self.client.put(self.url(), {'connection': connection.pk}, format='json')
        )
        self.assertTrue(data['connected'])
        self.assertEqual(data['channel']['keyword'], 'projectf')
        self.assertEqual(data['channel']['join_link'], 'https://wa.me/15550001111?text=START')

    def test_a_member_who_is_not_admin_cannot_attach(self):
        connection = make_connection(self.bob, phone_number_id='2003')
        self.client.force_authenticate(self.bob)
        body = self.assertFailure(
            self.client.put(self.url(), {'connection': connection.pk}, format='json'), 400
        )
        self.assertEqual(body['data']['reason'], 'not_admin')

    def test_nobody_can_attach_somebody_elses_number(self):
        connection = make_connection(self.bob)
        self.assertFailure(
            self.client.put(self.url(), {'connection': connection.pk}, format='json'), 404
        )

    def test_a_non_member_cannot_see_the_group(self):
        self.client.force_authenticate(make_user('mallory'))
        self.assertFailure(self.client.get(self.url()), 404)

    def test_one_number_can_serve_many_groups_with_distinct_keywords(self):
        connection = make_connection(self.owner)
        other = Conversation.start_group(self.owner, name='Project F')
        self.client.put(self.url(), {'connection': connection.pk}, format='json')
        data = self.assertSuccess(
            self.client.put(self.url(other), {'connection': connection.pk}, format='json')
        )
        self.assertNotEqual(data['channel']['keyword'], 'projectf')
        self.assertEqual(connection.group_channels.count(), 2)


# --- inviting ------------------------------------------------------------------


class InviteTests(WhatsAppTestBase):
    def invite(self, **body):
        return self.client.post(
            reverse('whatsapp:group-invite', args=[self.group.pk]), body, format='json'
        )

    def test_inviting_without_a_connection_sends_the_owner_to_connect(self):
        body = self.assertFailure(self.invite(phone_number='+15550009999', display_name='Dan'))
        self.assertEqual(body['data']['reason'], 'whatsapp_not_connected')
        self.assertFalse(Participant.objects.filter(phone_number='+15550009999').exists())

    @override_settings(WHATSAPP_INVITE_TEMPLATE_NAME='loopdn_invite')
    def test_inviting_attaches_the_number_adds_them_and_sends_the_template(self):
        make_connection(self.owner)
        data = self.assertSuccess(
            self.invite(phone_number='+15550009999', display_name='Dan'), 201
        )
        self.assertEqual(data['outcome'], 'invited')

        dan = Participant.objects.get(conversation=self.group, phone_number='+15550009999')
        self.assertEqual(membership.consent_status(dan, WHATSAPP), ChannelConsent.Status.PENDING)
        self.assertTrue(GroupChannel.whatsapp_for(self.group))

        sent = self.graph.sent[-1]
        self.assertEqual(sent['to'], '15550009999')
        self.assertEqual(sent['template']['name'], 'loopdn_invite')

    def test_without_a_template_the_invite_is_recorded_as_not_sent(self):
        make_connection(self.owner)
        data = self.assertSuccess(
            self.invite(phone_number='+15550009999', display_name='Dan'), 201
        )
        self.assertEqual(data['outcome'], 'not_sent')
        self.assertEqual(data['reason'], 'no invitation template configured')
        self.assertEqual(self.graph.sent, [])

    @override_settings(WHATSAPP_INVITE_TEMPLATE_NAME='loopdn_invite')
    def test_somebody_who_opted_out_is_not_asked_again(self):
        make_connection(self.owner)
        membership.set_channel_consent(self.bob_row, WHATSAPP, ChannelConsent.Status.OPTED_OUT)
        data = self.assertSuccess(self.invite(participant=self.bob_row.pk), 201)
        self.assertEqual(data['outcome'], 'declined')
        self.assertEqual(self.graph.sent, [])

    def test_member_opts_in_from_the_app(self):
        self.attach(make_connection(self.owner))
        self.client.force_authenticate(self.bob)
        data = self.assertSuccess(self.client.put(
            reverse('whatsapp:group-consent', args=[self.group.pk]), {'enabled': True},
            format='json',
        ))
        self.assertEqual(data['my_consent'], ChannelConsent.Status.OPTED_IN)

    def test_member_without_a_verified_number_cannot_opt_in(self):
        self.attach(make_connection(self.owner))
        self.bob.profile.phone_no_verified = False
        self.bob.profile.save()
        self.client.force_authenticate(self.bob)
        body = self.assertFailure(self.client.put(
            reverse('whatsapp:group-consent', args=[self.group.pk]), {'enabled': True},
            format='json',
        ))
        self.assertEqual(body['data']['reason'], 'no_verified_number')


# --- the fan-out -------------------------------------------------------------------


class FanOutTests(WhatsAppTestBase):
    def setUp(self):
        super().setUp()
        self.connection = make_connection(self.owner)
        self.channel = self.attach(self.connection)

    def post(self, text='Dinner at 7', by=None):
        return create_message(self.group, sender=by or self.owner, text=text)

    def open_window(self, wa_id):
        WhatsAppContact.objects.update_or_create(
            connection=self.connection, wa_id=wa_id,
            defaults={'last_inbound_at': timezone.now()},
        )

    def test_a_group_without_whatsapp_sends_nothing(self):
        self.channel.delete()
        self.assertEqual(outbound.fan_out(self.post()), [])

    def test_only_opted_in_members_are_messaged_and_others_leave_no_row(self):
        opt_in(self.bob_row)
        self.open_window('15550000002')
        rows = outbound.fan_out(self.post())

        self.assertEqual([r.participant for r in rows], [self.bob_row])
        self.assertEqual(rows[0].status, WhatsAppDelivery.Status.SENT)
        self.assertEqual(self.graph.sent[0]['text']['body'], 'Project F - fiona: Dinner at 7')

    def test_the_sender_is_never_messaged_their_own_message(self):
        owner_row = Participant.objects.get(conversation=self.group, user=self.owner)
        opt_in(owner_row)
        self.assertEqual(outbound.fan_out(self.post()), [])

    @override_settings(WHATSAPP_DEFAULT_TEMPLATE_NAME='loopdn_group_message')
    def test_outside_the_window_the_template_is_used(self):
        opt_in(self.bob_row)
        rows = outbound.fan_out(self.post())

        self.assertEqual(rows[0].kind, WhatsAppDelivery.Kind.TEMPLATE)
        template = self.graph.sent[0]['template']
        self.assertEqual(template['name'], 'loopdn_group_message')
        params = [p['text'] for p in template['components'][0]['parameters']]
        self.assertEqual(params, ['Project F', 'fiona', 'Dinner at 7'])

    def test_template_parameters_are_flattened(self):
        self.assertEqual(outbound.template_param('a\n\nb\t   c'), 'a b c')

    def test_outside_the_window_with_no_template_is_skipped_with_a_reason(self):
        opt_in(self.bob_row)
        rows = outbound.fan_out(self.post())
        self.assertEqual(rows[0].status, WhatsAppDelivery.Status.SKIPPED)
        self.assertIn('24-hour window', rows[0].reason)

    def test_a_retried_fan_out_does_not_message_anybody_twice(self):
        opt_in(self.bob_row)
        self.open_window('15550000002')
        message = self.post()
        outbound.fan_out(message)
        outbound.fan_out(message)
        self.assertEqual(len(self.graph.sent), 1)

    def test_a_muted_member_is_skipped_with_a_reason(self):
        opt_in(self.bob_row)
        membership.set_muted(self.bob_row, True)
        rows = outbound.fan_out(self.post())
        self.assertEqual(rows[0].status, WhatsAppDelivery.Status.SKIPPED)
        self.assertEqual(rows[0].reason, 'muted')

    def test_a_revoked_token_marks_the_connection_broken(self):
        from whatsapp.graph import WhatsAppError

        opt_in(self.bob_row)
        opt_in(self.carol_row)
        self.open_window('15550000002')
        self.open_window('15550000003')
        self.graph.send_error = WhatsAppError('bad token', code=190, permanent=True)

        rows = outbound.fan_out(self.post())
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, WhatsAppConnection.Status.ERROR)
        self.assertTrue(all(r.status == WhatsAppDelivery.Status.FAILED for r in rows))
        # Stopped after the first failure rather than hammering Meta.
        sends = [c for c in self.graph.calls if c[1].endswith('/messages')]
        self.assertEqual(len(sends), 1)

    def test_a_delivered_message_moves_the_recipients_current_group(self):
        opt_in(self.bob_row)
        self.open_window('15550000002')
        outbound.fan_out(self.post())
        contact = WhatsAppContact.objects.get(wa_id='15550000002')
        self.assertEqual(contact.current_conversation, self.group)


# --- the webhook -------------------------------------------------------------------


def signed(body):
    raw = json.dumps(body).encode()
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, f'sha256={digest}'


def message_event(text, *, from_='15550000002', wamid='wamid.in1', context=None,
                  phone_number_id='2002', kind='text'):
    item = {'from': from_, 'id': wamid, 'timestamp': '1', 'type': kind}
    if kind == 'text':
        item['text'] = {'body': text}
    if context:
        item['context'] = {'id': context}
    return {'object': 'whatsapp_business_account', 'entry': [{'id': '1001', 'changes': [{
        'field': 'messages',
        'value': {
            'metadata': {'phone_number_id': phone_number_id},
            'contacts': [{'wa_id': from_, 'profile': {'name': 'Bob'}}],
            'messages': [item],
        },
    }]}]}


class WebhookTests(WhatsAppTestBase):
    def setUp(self):
        super().setUp()
        self.connection = make_connection(self.owner)
        self.channel = self.attach(self.connection)
        self.client.force_authenticate(None)

    def deliver(self, body, signature=None):
        raw, good = signed(body)
        return self.client.post(
            reverse('whatsapp:webhook'), raw, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=signature if signature is not None else good,
        )

    def replies(self):
        return [p['text']['body'] for p in self.graph.sent if p.get('type') == 'text']

    def test_meta_verifies_the_url(self):
        response = self.client.get(reverse('whatsapp:webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': 'verify-me', 'hub.challenge': '42',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'42')

    def test_a_wrong_verify_token_is_refused(self):
        response = self.client.get(reverse('whatsapp:webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': 'nope', 'hub.challenge': '42',
        })
        self.assertEqual(response.status_code, 403)

    def test_a_bad_signature_is_refused_and_nothing_is_posted(self):
        opt_in(self.bob_row)
        response = self.deliver(message_event('hi'), signature='sha256=' + '0' * 64)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Message.objects.exists())

    def test_an_opted_in_member_posts_into_the_group(self):
        opt_in(self.bob_row)
        self.assertEqual(self.deliver(message_event('On my way')).status_code, 200)

        message = Message.objects.get()
        self.assertEqual(message.conversation, self.group)
        self.assertEqual(message.sender, self.bob)
        self.assertEqual(message.source, Message.Source.WHATSAPP)
        self.assertEqual(message.text, 'On my way')
        # And their window is open now.
        self.assertTrue(outbound.in_window(self.connection, '15550000002'))

    def test_a_retried_webhook_does_not_post_twice(self):
        opt_in(self.bob_row)
        self.deliver(message_event('On my way'))
        self.deliver(message_event('On my way'))
        self.assertEqual(Message.objects.count(), 1)

    def test_a_stranger_is_ignored_silently(self):
        self.deliver(message_event('hello?', from_='15559999999'))
        self.assertFalse(Message.objects.exists())
        self.assertEqual(self.graph.sent, [])

    def test_a_number_we_do_not_serve_is_ignored(self):
        opt_in(self.bob_row)
        self.assertEqual(
            self.deliver(message_event('hi', phone_number_id='2998')).status_code, 200
        )
        self.assertFalse(Message.objects.exists())

    def test_a_member_who_never_opted_in_is_asked_to_send_start(self):
        self.deliver(message_event('hello'))
        self.assertFalse(Message.objects.exists())
        self.assertIn('Reply START', self.replies()[0])

    def test_start_opts_in_and_stop_opts_out(self):
        self.deliver(message_event('START', wamid='w1'))
        self.assertEqual(membership.consent_status(self.bob_row, WHATSAPP),
                         ChannelConsent.Status.OPTED_IN)

        self.deliver(message_event('stop', wamid='w2'))
        self.assertEqual(membership.consent_status(self.bob_row, WHATSAPP),
                         ChannelConsent.Status.OPTED_OUT)
        self.assertFalse(Message.objects.exists())

    def test_yes_is_a_message_not_a_command(self):
        opt_in(self.bob_row)
        self.deliver(message_event('Yes'))
        self.assertEqual(Message.objects.get().text, 'Yes')

    def test_media_is_not_posted_and_the_sender_is_told(self):
        opt_in(self.bob_row)
        self.deliver(message_event('', kind='image'))
        self.assertFalse(Message.objects.exists())
        self.assertIn('Only text', self.replies()[0])

    def test_status_updates_move_forward_only(self):
        row = WhatsAppDelivery.objects.create(
            direction=WhatsAppDelivery.Direction.OUTBOUND, connection=self.connection,
            conversation=self.group, participant=self.bob_row, wamid='wamid.out',
            status=WhatsAppDelivery.Status.SENT,
        )

        def status_event(value):
            return {'entry': [{'changes': [{'field': 'messages', 'value': {
                'metadata': {'phone_number_id': '2002'},
                'statuses': [{'id': 'wamid.out', 'status': value,
                              'pricing': {'category': 'utility'}}],
            }}]}]}

        self.deliver(status_event('read'))
        self.deliver(status_event('delivered'))
        row.refresh_from_db()
        self.assertEqual(row.status, WhatsAppDelivery.Status.READ)
        self.assertEqual(row.pricing_category, 'utility')

    def test_removing_our_app_in_business_manager_disconnects(self):
        self.deliver({'entry': [{'id': '1001', 'changes': [{
            'field': 'account_update', 'value': {'event': 'PARTNER_REMOVED'},
        }]}]})
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, WhatsAppConnection.Status.DISCONNECTED)
        self.assertFalse(GroupChannel.objects.exists())

    def test_a_failed_item_releases_its_claim_so_the_retry_works(self):
        opt_in(self.bob_row)
        with mock.patch('whatsapp.inbound.create_message', side_effect=RuntimeError):
            self.assertEqual(self.deliver(message_event('hi')).status_code, 500)
        self.assertFalse(WhatsAppWebhookEvent.objects.exists())

        self.deliver(message_event('hi'))
        self.assertEqual(Message.objects.count(), 1)


# --- routing one number across several groups -------------------------------------


class RoutingTests(WebhookTests.__base__):
    """Bob is in Project F and Team F, both on Fiona's one number."""

    def setUp(self):
        super().setUp()
        self.connection = make_connection(self.owner)
        self.first = self.attach(self.connection)
        self.team = Conversation.start_group(self.owner, name='Team F')
        self.team.add_participant(self.bob)
        self.second = self.attach(self.connection, self.team)
        opt_in(self.bob_row)
        opt_in(Participant.objects.get(conversation=self.team, user=self.bob))
        self.client.force_authenticate(None)

    deliver = WebhookTests.deliver
    replies = WebhookTests.replies

    def test_an_unaddressed_message_asks_which_group(self):
        self.deliver(message_event('hello'))
        self.assertFalse(Message.objects.exists())
        self.assertIn('#projectf', self.replies()[0])
        self.assertIn('#teamf', self.replies()[0])

    def test_a_keyword_picks_the_group_and_sticks(self):
        self.deliver(message_event('#teamf practice moved', wamid='w1'))
        self.deliver(message_event('see you there', wamid='w2'))

        texts = list(Message.objects.filter(conversation=self.team).values_list('text', flat=True))
        self.assertEqual(sorted(texts), ['practice moved', 'see you there'])

    def test_a_swipe_reply_goes_to_the_quoted_messages_group(self):
        WhatsAppDelivery.objects.create(
            direction=WhatsAppDelivery.Direction.OUTBOUND, connection=self.connection,
            conversation=self.team, wamid='wamid.team-msg',
        )
        self.deliver(message_event('yes!', context='wamid.team-msg'))
        self.assertEqual(Message.objects.get().conversation, self.team)

    def test_a_reply_to_a_group_they_left_is_not_posted_there(self):
        WhatsAppDelivery.objects.create(
            direction=WhatsAppDelivery.Direction.OUTBOUND, connection=self.connection,
            conversation=self.team, wamid='wamid.team-msg',
        )
        membership.leave(Participant.objects.get(conversation=self.team, user=self.bob))
        self.deliver(message_event('hi', context='wamid.team-msg'))
        # Only Project F is left, so it goes there - never to Team F.
        self.assertEqual(Message.objects.get().conversation, self.group)

    def test_an_unknown_hashtag_is_just_text(self):
        WhatsAppContact.objects.create(
            connection=self.connection, wa_id='15550000002', current_conversation=self.group,
        )
        self.deliver(message_event('#1 fan here'))
        self.assertEqual(Message.objects.get().text, '#1 fan here')


class CryptoTests(TestCase):
    @override_settings(WHATSAPP_TOKEN_KEYS=[])
    def test_round_trip(self):
        self.assertEqual(crypto.decrypt(crypto.encrypt('secret')), 'secret')

    def test_rotation_reads_old_tokens(self):
        from cryptography.fernet import Fernet

        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with override_settings(WHATSAPP_TOKEN_KEYS=[old]):
            stored = crypto.encrypt('secret')
        with override_settings(WHATSAPP_TOKEN_KEYS=[new, old]):
            self.assertEqual(crypto.decrypt(stored), 'secret')
