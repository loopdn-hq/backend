"""Tests for the Telnyx client, split across its two products.

The move out of `utils/sms.py` is meant to be a pure relocation, so the real
proof lives in `otp/tests.py`, which drives both phone endpoints end to end and
still passes untouched. What is asserted here is the one thing that move could
have broken quietly.
"""
from django.contrib.auth.models import User
from django.test import TestCase

import sms.client
import sms.verification
from chat.models import (
    BlockedFromConversation,
    Conversation,
    Message,
    Participant,
)
import base64
import time
import urllib.parse
from unittest import mock

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

import sms.client
from chat import membership
from chat.services import create_message
from user.tests import make_image_upload
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from sms import (
    commands,
    inbound,
    maintenance,
    outbound,
    provisioning,
    segments,
    signature,
)
from sms.models import GroupNumber, SmsDelivery, SmsSettings, SmsWebhookEvent


class SmsErrorIdentityTests(TestCase):
    """There must be exactly one SmsError class in the project.

    `loopd/exceptions.py` maps SmsError to a 503 with a single `isinstance`
    check. A second copy of the class - the obvious mistake when splitting one
    module into two - would not match it, and a Telnyx outage would surface as
    a 500 with no explanation instead of "not your fault, try again".
    """

    def test_both_modules_share_one_error_class(self):
        self.assertIs(sms.verification.SmsError, sms.client.SmsError)

    def test_the_exception_handler_maps_the_same_class(self):
        from loopd.exceptions import SmsError as handler_error

        self.assertIs(handler_error, sms.client.SmsError)

    def test_utils_no_longer_defines_one(self):
        # utils/sms.py is deleted, not emptied: a leftover module would keep
        # importing cleanly and quietly shadow the real class for anyone who
        # had not updated their import yet.
        with self.assertRaises(ImportError):
            __import__('utils.sms')


class CommandParsingTests(TestCase):
    """A command is the whole message, never a word inside one."""

    def assertCommand(self, body, expected, argument=None):
        self.assertEqual(commands.parse(body), (expected, argument))

    def test_the_plain_keywords(self):
        for body, expected in [
            ('STOP', commands.STOP),
            ('stop', commands.STOP),
            ('  Stop  ', commands.STOP),
            ('STOP.', commands.STOP),
            ('UNSUBSCRIBE', commands.STOP),
            ('CANCEL', commands.STOP),
            ('QUIT', commands.STOP),
            ('START', commands.START),
            ('YES', commands.START),
            ('HELP', commands.HELP),
            ('info', commands.HELP),
            ('MUTE', commands.MUTE),
            ('unmute', commands.UNMUTE),
            ('LEAVE', commands.LEAVE),
        ]:
            with self.subTest(body=body):
                self.assertCommand(body, expected)

    def test_a_sentence_containing_a_keyword_is_not_a_command(self):
        # The bug this prevents: somebody texts their family "stop by the shop"
        # and is silently unsubscribed from the group with no explanation.
        for body in [
            'stop by the shop on your way home',
            'can you help me with dinner',
            'I will leave at 6',
            'Please start the laundry',
            'no',
            'yes please bring milk',
        ]:
            with self.subTest(body=body):
                self.assertCommand(body, None)

    def test_join_carries_a_code(self):
        self.assertCommand('JOIN', commands.JOIN)
        self.assertCommand('JOIN FAMILY', commands.JOIN, 'FAMILY')
        self.assertCommand('join family', commands.JOIN, 'family')
        self.assertCommand('  Join   AB2CD9 ', commands.JOIN, 'AB2CD9')

    def test_an_empty_message_is_not_a_command(self):
        for body in ['', '   ', None, '...']:
            with self.subTest(body=body):
                self.assertCommand(body, None)


class InboundSmsTests(TestCase):
    """One text arriving for a group, and what it does."""

    def setUp(self):
        # The join-attempt and reply caps live in the cache, which is shared
        # across tests in a class. Without this, the tests later in the file
        # trip a rate limit set by the ones before them.
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.number = '+639171111111'

    def send(self, body, number=None, **kwargs):
        return inbound.handle_inbound(
            self.group, number or self.number, body, **kwargs
        )

    def join(self, mode=Conversation.JoinMode.OPEN, **kwargs):
        self.group.join_mode = mode
        self.group.save(update_fields=['join_mode'])
        return self.send('JOIN', **kwargs)

    # --- joining ------------------------------------------------------------

    def test_texting_join_to_an_open_group_gets_you_in(self):
        result = self.join(display_name='Mom')
        self.assertEqual(result.action, 'joined')
        self.assertTrue(self.group.has_phone_participant(self.number))
        # They texted us first, so consent is not in doubt.
        self.assertEqual(result.participant.sms_consent, Participant.Consent.GRANTED)
        self.assertTrue(result.participant.sms_enabled)

    def test_join_on_an_approval_group_waits_for_an_admin(self):
        result = self.join(mode=Conversation.JoinMode.APPROVAL)
        self.assertEqual(result.action, 'join_pending')
        self.assertEqual(result.participant.status, Participant.Status.PENDING)
        self.assertFalse(self.group.has_phone_participant(self.number))

    def test_join_needs_the_code_when_the_group_asks_for_one(self):
        self.group.join_mode = Conversation.JoinMode.CODE
        self.group.save(update_fields=['join_mode'])

        self.assertEqual(self.send('JOIN').action, 'refused')
        self.assertEqual(self.send('JOIN WRONG').action, 'refused')
        self.assertEqual(
            self.send(f'JOIN {self.group.join_code}').action, 'joined'
        )

    def test_an_invite_only_group_cannot_be_joined_by_text(self):
        result = self.join(mode=Conversation.JoinMode.INVITE)
        self.assertEqual(result.action, 'refused')
        self.assertFalse(self.group.has_phone_participant(self.number))

    def test_a_blocked_number_gets_silence_not_an_explanation(self):
        self.join()
        row = self.group.participants.get(phone_number=self.number)
        row.status = Participant.Status.BLOCKED
        row.save(update_fields=['status'])

        result = self.send('JOIN')
        self.assertEqual(result.action, 'refused_blocked')
        # Silence: telling them invites a retry from another number.
        self.assertIsNone(result.reply)

    # --- posting ------------------------------------------------------------

    def test_a_member_texting_posts_to_the_group(self):
        self.join(display_name='Mom')
        result = self.send('Dinner at 7')

        self.assertEqual(result.action, 'posted')
        self.assertEqual(result.message.text, 'Dinner at 7')
        self.assertEqual(result.message.source, Message.Source.SMS)
        self.assertIsNone(result.message.sender_id)
        self.assertEqual(result.message.sender_participant.display_label, 'Mom')
        # The sender's own phone already shows it; a reply would just cost money.
        self.assertIsNone(result.reply)

    def test_a_stranger_is_ignored_in_silence(self):
        result = self.send('let me in')
        self.assertEqual(result.action, 'ignored_unknown_sender')
        self.assertIsNone(result.reply)
        self.assertEqual(Message.objects.count(), 0)

    def test_a_pending_member_cannot_post_yet(self):
        self.join(mode=Conversation.JoinMode.APPROVAL)
        result = self.send('am I in?')
        self.assertEqual(result.action, 'ignored_unknown_sender')
        self.assertEqual(Message.objects.count(), 0)

    def test_an_empty_message_posts_nothing(self):
        self.join()
        self.assertEqual(self.send('   ').action, 'ignored_empty')
        self.assertEqual(Message.objects.count(), 0)

    # --- membership commands ------------------------------------------------

    def test_mute_and_unmute(self):
        self.join()
        self.assertEqual(self.send('MUTE').action, 'muted')
        self.assertTrue(self.group.participants.get(phone_number=self.number).muted)
        self.assertEqual(self.send('UNMUTE').action, 'unmuted')
        self.assertFalse(self.group.participants.get(phone_number=self.number).muted)

    def test_leave_ends_membership_but_keeps_the_messages(self):
        self.join()
        self.send('I was here')
        self.assertEqual(self.send('LEAVE').action, 'left')
        self.assertFalse(self.group.has_phone_participant(self.number))
        self.assertTrue(Message.objects.filter(text='I was here').exists())

    # --- carrier-mandated keywords -----------------------------------------

    def test_stop_silences_the_number_without_removing_them(self):
        self.join()
        result = self.send('STOP')

        self.assertEqual(result.action, 'opted_out')
        # No reply: the carrier sends the mandated confirmation itself, and a
        # second one is a double-ack and a second charge.
        self.assertIsNone(result.reply)

        row = self.group.participants.get(phone_number=self.number)
        self.assertFalse(row.sms_enabled)
        self.assertEqual(row.sms_consent, Participant.Consent.STOPPED)
        # STOP is about texts, not membership.
        self.assertEqual(row.status, Participant.Status.ACTIVE)

    def test_stop_is_honoured_even_from_somebody_who_is_not_a_member(self):
        # A carrier audit tests this literally, from an unknown number.
        result = self.send('STOP', number='+639179999999')
        self.assertEqual(result.action, 'opted_out')

    def test_start_puts_them_back(self):
        self.join()
        self.send('STOP')
        result = self.send('START')

        self.assertEqual(result.action, 'opted_in')
        row = self.group.participants.get(phone_number=self.number)
        self.assertTrue(row.sms_enabled)
        self.assertEqual(row.sms_consent, Participant.Consent.GRANTED)

    def test_help_answers_in_one_gsm7_segment(self):
        self.join()
        result = self.send('HELP')
        self.assertEqual(result.action, 'help')
        self.assertTrue(result.reply)
        self.assertLessEqual(len(result.reply), 160)

    # --- identifying the sender --------------------------------------------

    def test_an_account_member_is_recognised_by_their_verified_number(self):
        bob = User.objects.create_user(username='bob', password='x')
        bob.profile.phone_number = '+639172222222'
        bob.profile.phone_no_verified = True
        bob.profile.save()
        self.group.add_participant(bob)

        result = self.send('from the app user', number='+639172222222')
        self.assertEqual(result.action, 'posted')
        self.assertEqual(result.message.sender_id, bob.pk)

    def test_an_unverified_number_is_not_recognised(self):
        # The sending number is the only credential an SMS carries, so an
        # unproven claim on a number must not be able to post as its owner.
        eve = User.objects.create_user(username='eve', password='x')
        eve.profile.phone_number = '+639173333333'
        eve.profile.save()
        self.group.add_participant(eve)

        result = self.send('posting as eve', number='+639173333333')
        self.assertEqual(result.action, 'ignored_unknown_sender')
        self.assertEqual(Message.objects.count(), 0)


class SegmentCountingTests(TestCase):
    """What a message actually costs. Every answer here is money."""

    def test_a_short_gsm7_message_is_one_segment(self):
        self.assertEqual(segments.count_segments('Dinner at 7'), (1, segments.GSM7))

    def test_the_gsm7_boundary(self):
        self.assertEqual(segments.count_segments('a' * 160), (1, segments.GSM7))
        # 161 characters do not become 2x160; concatenation steals 7 per part.
        self.assertEqual(segments.count_segments('a' * 161), (2, segments.GSM7))
        self.assertEqual(segments.count_segments('a' * 306), (2, segments.GSM7))
        self.assertEqual(segments.count_segments('a' * 307), (3, segments.GSM7))

    def test_one_curly_quote_doubles_the_cost_of_a_whole_message(self):
        # This is the bug the module exists to prevent: a phone inserts a curly
        # apostrophe on its own, and every message in that group costs double.
        plain = 'a' * 100 + "'"
        curly = 'a' * 100 + '’'
        self.assertEqual(segments.count_segments(plain), (1, segments.GSM7))
        self.assertEqual(segments.count_segments(curly), (2, segments.UCS2))

    def test_an_em_dash_is_not_gsm7(self):
        # The exact character that would otherwise sit in the message template.
        self.assertFalse(segments.is_gsm7('Family — Yul: hi'))
        self.assertTrue(segments.is_gsm7('Family - Yul: hi'))

    def test_extension_characters_cost_two(self):
        # 80 of them is 160 slots: still one segment. 81 is not.
        self.assertEqual(segments.count_segments('[' * 80), (1, segments.GSM7))
        self.assertEqual(segments.count_segments('[' * 81), (2, segments.GSM7))

    def test_emoji_are_counted_in_utf16_units_not_characters(self):
        # len() says 40; the wire says 80. Counting characters would under-report
        # every message with an emoji in it - which is most of them.
        body = '\U0001F600' * 40
        self.assertEqual(len(body), 40)
        self.assertEqual(segments.count_segments(body), (2, segments.UCS2))

    def test_an_empty_message_still_costs_one(self):
        self.assertEqual(segments.count_segments(''), (1, segments.GSM7))


class FanOutTests(TestCase):
    """One message in, many texts out - and mostly, deciding not to send."""

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.number = GroupNumber.objects.create(
            number='+14155551001',
            conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.sender_row = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

    def add_phone_member(self, number, name='Mom', **kwargs):
        row = self.group.add_phone_participant(number, display_name=name)
        row.sms_consent = Participant.Consent.GRANTED
        for field, value in kwargs.items():
            setattr(row, field, value)
        row.save()
        return row

    def post(self, text='Dinner at 7'):
        return create_message(
            conversation=self.group,
            sender=self.alice,
            text=text,
            participant=self.sender_row,
        )

    def test_a_phone_member_is_texted_and_the_delivery_logged(self):
        self.add_phone_member('+639171111111')
        message = self.post()

        rows = outbound.fan_out(message)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, SmsDelivery.Status.SENT)
        self.assertEqual(rows[0].direction, SmsDelivery.Direction.OUTBOUND)
        self.assertEqual(rows[0].segments, 1)
        # The number is never stored in full.
        self.assertEqual(rows[0].counterpart_last4, '1111')
        self.assertNotIn('639171111111', rows[0].counterpart_hash)

    def test_the_sender_never_gets_their_own_message(self):
        self.alice.profile.phone_number = '+639170000001'
        self.alice.profile.phone_no_verified = True
        self.alice.profile.save()
        Participant.objects.filter(pk=self.sender_row.pk).update(sms_enabled=True)

        rows = outbound.fan_out(self.post())
        self.assertEqual([r.participant_id for r in rows], [])

    def test_the_message_names_its_group_and_sender(self):
        message = self.post()
        body = outbound.format_message(self.group, message)
        self.assertEqual(body, 'Family - alice: Dinner at 7')
        # And the fixed parts must stay cheap to send.
        self.assertTrue(segments.is_gsm7('Family - alice: '))

    def test_every_reason_not_to_send_is_recorded_rather_than_silent(self):
        muted = self.add_phone_member('+639171111111', name='Muted', muted=True)
        off = self.add_phone_member('+639172222222', name='Off', sms_enabled=False)
        stopped = self.add_phone_member('+639173333333', name='Stopped')
        stopped.sms_consent = Participant.Consent.STOPPED
        stopped.save(update_fields=['sms_consent'])

        rows = {r.participant_id: r for r in outbound.fan_out(self.post())}

        for row, expected in [
            (muted, 'muted'),
            (off, 'sms disabled for this group'),
            (stopped, 'opted out'),
        ]:
            self.assertEqual(rows[row.pk].status, SmsDelivery.Status.SKIPPED)
            self.assertEqual(rows[row.pk].reason, expected)

    def test_a_pending_member_is_not_texted_at_all(self):
        pending = self.add_phone_member('+639171111111')
        Participant.objects.filter(pk=pending.pk).update(
            status=Participant.Status.PENDING
        )
        self.assertEqual(outbound.fan_out(self.post()), [])

    def test_a_group_with_no_number_records_why_it_sent_nothing(self):
        self.add_phone_member('+639171111111')
        GroupNumber.objects.filter(pk=self.number.pk).update(conversation=None)

        rows = outbound.fan_out(self.post())
        self.assertEqual(rows[0].status, SmsDelivery.Status.SKIPPED)
        self.assertEqual(rows[0].reason, 'group has no number')

    def test_the_daily_budget_stops_sending(self):
        cache.clear()
        self.add_phone_member('+639171111111')
        with override_settings(SMS_MAX_SEGMENTS_PER_GROUP_PER_DAY=0):
            rows = outbound.fan_out(self.post())
        self.assertEqual(rows[0].status, SmsDelivery.Status.SKIPPED)
        self.assertIn('budget', rows[0].reason)

    def test_one_bad_number_does_not_stop_the_others(self):
        self.add_phone_member('+639171111111', name='Good')
        self.add_phone_member('+639172222222', name='Bad')
        message = self.post()

        real_send = sms.client.send_text

        def flaky(*, to, from_number, text):
            if to == '+639172222222':
                raise sms.client.SmsError('carrier rejected')
            return real_send(to=to, from_number=from_number, text=text)

        with mock.patch('sms.outbound.send_text', flaky):
            rows = {r.counterpart_last4: r for r in outbound.fan_out(message)}

        self.assertEqual(rows['1111'].status, SmsDelivery.Status.SENT)
        self.assertEqual(rows['2222'].status, SmsDelivery.Status.FAILED)
        self.assertEqual(rows['2222'].reason, 'carrier rejected')


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public).decode()


class WebhookSignatureTests(TestCase):
    """Pure signature checking - a keypair, a fixture, no network."""

    def setUp(self):
        self.private, self.public_b64 = _keypair()
        self.body = b'{"data":{"id":"evt_1"}}'

    def sign(self, body=None, timestamp=None):
        body = self.body if body is None else body
        timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
        sig = self.private.sign(timestamp.encode() + b'.' + body)
        return timestamp, base64.b64encode(sig).decode()

    def test_a_genuine_signature_is_accepted(self):
        ts, sig = self.sign()
        with override_settings(TELNYX_PUBLIC_KEYS=[self.public_b64]):
            self.assertTrue(signature.verify(self.body, ts, sig))

    def test_a_tampered_body_is_refused(self):
        ts, sig = self.sign()
        with override_settings(TELNYX_PUBLIC_KEYS=[self.public_b64]):
            self.assertFalse(signature.verify(b'{"data":{"id":"evil"}}', ts, sig))

    def test_a_stale_timestamp_is_refused(self):
        # Otherwise a captured request can be replayed forever.
        ts, sig = self.sign(timestamp=int(time.time()) - 4000)
        with override_settings(TELNYX_PUBLIC_KEYS=[self.public_b64]):
            self.assertFalse(signature.verify(self.body, ts, sig))

    def test_the_second_of_two_keys_may_match(self):
        # Rotating a signing key needs both valid at once.
        _, other_public = _keypair()
        ts, sig = self.sign()
        with override_settings(TELNYX_PUBLIC_KEYS=[other_public, self.public_b64]):
            self.assertTrue(signature.verify(self.body, ts, sig))

    def test_with_no_keys_configured_nothing_is_checked(self):
        # Development only, and keyed on the absence of keys rather than on a
        # boolean a staging deploy could inherit.
        with override_settings(TELNYX_PUBLIC_KEYS=[]):
            self.assertTrue(signature.verify(self.body, '', ''))


@override_settings(TELNYX_PUBLIC_KEYS=[])
class InboundWebhookTests(APITestCase):
    """A real text arriving over HTTP, end to end."""

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.join_mode = Conversation.JoinMode.OPEN
        self.group.save(update_fields=['join_mode'])
        GroupNumber.objects.create(
            number='+14155551001',
            conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.url = reverse('sms:inbound-webhook')

    def deliver(self, text, from_number='+639171111111', to='+14155551001',
                event_id='evt_1'):
        return self.client.post(
            self.url,
            {
                'data': {
                    'id': event_id,
                    'event_type': 'message.received',
                    'payload': {
                        'from': {'phone_number': from_number},
                        'to': [{'phone_number': to}],
                        'text': text,
                    },
                }
            },
            format='json',
        )

    def test_a_text_to_an_unrouted_number_is_accepted_and_ignored(self):
        # 200, because a retry would not help - and no reply, because answering
        # tells whoever is probing that the number is live.
        response = self.deliver('hello', to='+14155559999')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Message.objects.count(), 0)

    def test_a_stranger_texting_join_becomes_a_member(self):
        self.assertEqual(self.deliver('JOIN').status_code, status.HTTP_200_OK)
        self.assertTrue(self.group.has_phone_participant('+639171111111'))

    def test_a_member_texting_lands_in_the_group_conversation(self):
        self.deliver('JOIN', event_id='evt_join')
        self.deliver('Dinner at 7', event_id='evt_msg')

        message = Message.objects.get()
        self.assertEqual(message.text, 'Dinner at 7')
        self.assertEqual(message.source, Message.Source.SMS)
        self.assertEqual(message.conversation_id, self.group.pk)
        # And the inbound leg is logged - it costs money too.
        self.assertTrue(
            SmsDelivery.objects.filter(
                direction=SmsDelivery.Direction.INBOUND, message=message
            ).exists()
        )

    def test_a_retried_event_does_not_post_the_message_twice(self):
        # A provider resends anything it does not read as success.
        self.deliver('JOIN', event_id='evt_join')
        self.deliver('Dinner at 7', event_id='evt_same')
        second = self.deliver('Dinner at 7', event_id='evt_same')

        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(Message.objects.count(), 1)
        # Two distinct event ids were sent; the repeat was recognised, not stored.
        self.assertEqual(SmsWebhookEvent.objects.count(), 2)

    def test_a_bad_signature_is_refused(self):
        with override_settings(TELNYX_PUBLIC_KEYS=[_keypair()[1]]):
            response = self.deliver('hello')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Message.objects.count(), 0)

    def test_a_delivery_receipt_updates_the_log(self):
        row = SmsDelivery.objects.create(
            direction=SmsDelivery.Direction.OUTBOUND,
            conversation=self.group,
            status=SmsDelivery.Status.SENT,
            provider_message_id='msg_42',
        )
        self.client.post(
            self.url,
            {
                'data': {
                    'id': 'evt_dlr',
                    'event_type': 'message.finalized',
                    'payload': {'id': 'msg_42', 'to': [{'status': 'delivered'}],
                                'parts': 2},
                }
            },
            format='json',
        )
        row.refresh_from_db()
        self.assertEqual(row.status, SmsDelivery.Status.DELIVERED)
        self.assertEqual(row.segments, 2)
        self.assertIsNotNone(row.delivered_at)

    def test_a_malformed_payload_is_accepted_not_retried_forever(self):
        response = self.client.post(
            self.url, 'not json at all', content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class NumberPrivacyTests(TestCase):
    """A member's phone number must not be published to the other members.

    The label of somebody who joined by texting JOIN goes out in the BODY of
    every message the group sends. Falling back to their raw number there hands
    each member's number to everybody else - the exact thing a per-group number
    exists to avoid.
    """

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.join_mode = Conversation.JoinMode.OPEN
        self.group.save(update_fields=['join_mode'])

    def test_a_join_by_text_is_labelled_with_a_masked_number(self):
        result = inbound.handle_inbound(self.group, '+639171111111', 'JOIN')
        self.assertNotIn('+639171111111', result.participant.display_label)
        self.assertIn('1111', result.participant.display_label)

    def test_the_outbound_body_never_carries_a_full_number(self):
        inbound.handle_inbound(self.group, '+639171111111', 'JOIN')
        posted = inbound.handle_inbound(self.group, '+639171111111', 'Dinner at 7')

        body = outbound.format_message(self.group, posted.message)
        self.assertNotIn('+639171111111', body)
        self.assertIn('Dinner at 7', body)

    def test_a_name_given_when_adding_still_wins(self):
        row = self.group.add_phone_participant('+639172222222', display_name='Mom')
        self.assertEqual(row.display_label, 'Mom')


class WiredFanOutTests(TransactionTestCase):
    """The fan-out as it really runs: scheduled by create_message, on commit.

    TransactionTestCase, deliberately. A normal TestCase wraps each test in a
    transaction that is rolled back, so `transaction.on_commit` NEVER FIRES -
    which means every other fan-out test in this file is exercising a manual
    call, not the path production uses. This class is the one that proves the
    wiring itself.
    """

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        GroupNumber.objects.create(
            number='+14155551001',
            conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.mom = self.group.add_phone_participant('+639171111111', display_name='Mom')
        self.mom.sms_consent = Participant.Consent.GRANTED
        self.mom.save()
        self.sender = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

    def test_posting_fans_out_without_anyone_calling_it(self):
        create_message(
            conversation=self.group,
            sender=self.alice,
            text='Dinner at 7',
            participant=self.sender,
        )
        row = SmsDelivery.objects.get(
            direction=SmsDelivery.Direction.OUTBOUND, participant=self.mom
        )
        self.assertEqual(row.status, SmsDelivery.Status.SENT)

    def test_it_fans_out_exactly_once(self):
        message = create_message(
            conversation=self.group,
            sender=self.alice,
            text='Dinner at 7',
            participant=self.sender,
        )
        # Running it again - a retried worker, a replayed webhook - must not
        # text everybody a second time, and must not raise either.
        self.assertEqual(outbound.fan_out(message), [])
        self.assertEqual(
            SmsDelivery.objects.filter(
                direction=SmsDelivery.Direction.OUTBOUND, participant=self.mom
            ).count(),
            1,
        )

    def test_a_photo_with_no_caption_says_so(self):
        # Otherwise the recipient gets "Family - alice:" and no idea why.
        message = create_message(
            conversation=self.group,
            sender=self.alice,
            text='',
            participant=self.sender,
        )
        body = outbound.format_message(self.group, message)
        # The property that matters: never a dangling colon with nothing after
        # it, which is what a recipient used to receive.
        self.assertFalse(body.rstrip().endswith(':'), body)
        self.assertTrue(body.rstrip().endswith(')'), body)

    def test_a_photo_message_tells_the_recipient_to_open_the_app(self):
        message = create_message(
            conversation=self.group,
            sender=self.alice,
            text='',
            uploads=[make_image_upload('snap.png')],
            participant=self.sender,
        )
        body = outbound.format_message(self.group, message)
        self.assertIn('photo', body)
        self.assertIn('LoopD', body)

    def test_sending_can_be_switched_off_entirely(self):
        with override_settings(SMS_SENDING_ENABLED=False):
            create_message(
                conversation=self.group,
                sender=self.alice,
                text='Dinner at 7',
                participant=self.sender,
            )
        self.assertEqual(SmsDelivery.objects.count(), 0)


class KillSwitchAndDispatchTests(TestCase):
    """The runtime switch, and where the fan-out actually runs."""

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        GroupNumber.objects.create(
            number='+14155551001',
            conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.mom = self.group.add_phone_participant('+639171111111', display_name='Mom')
        self.mom.sms_consent = Participant.Consent.GRANTED
        self.mom.save()
        self.sender = Participant.objects.get(conversation=self.group, user=self.alice)

    def post(self):
        return create_message(
            conversation=self.group, sender=self.alice, text='hi',
            participant=self.sender,
        )

    def test_sending_is_on_by_default_with_no_row(self):
        # A fresh install has no settings row and must behave normally.
        self.assertTrue(SmsSettings.sending_is_on())

    def test_the_switch_stops_everything_and_says_why(self):
        SmsSettings.objects.create(singleton=True, sending_enabled=False,
                                   note='runaway loop')
        rows = outbound.fan_out(self.post())
        self.assertEqual(rows[0].status, SmsDelivery.Status.SKIPPED)
        self.assertEqual(rows[0].reason, 'sending is switched off')

    def test_flipping_the_switch_takes_effect_without_a_deploy(self):
        row = SmsSettings.objects.create(singleton=True, sending_enabled=False)
        self.assertFalse(SmsSettings.sending_is_on())

        row.sending_enabled = True
        row.save()
        # save() drops the cache, so the change is live rather than waiting out
        # a cache TTL - which is the whole point of a runtime switch.
        self.assertTrue(SmsSettings.sending_is_on())

    def test_without_a_broker_the_fan_out_runs_inline(self):
        with override_settings(CELERY_BROKER_URL='', REDIS_URL=''):
            with mock.patch('sms.outbound.dispatch') as inline:
                outbound._dispatch_somewhere(1)
        inline.assert_called_once_with(1)

    def test_with_a_broker_it_is_handed_to_a_worker(self):
        with override_settings(
            CELERY_BROKER_URL='redis://localhost:6379/1', REDIS_URL='redis://x'
        ):
            with mock.patch('sms.tasks.fan_out_message.delay') as queued:
                outbound._dispatch_somewhere(7)
        queued.assert_called_once_with(7)

    def test_an_unreachable_broker_falls_back_rather_than_losing_the_message(self):
        with override_settings(
            CELERY_BROKER_URL='redis://localhost:6379/1', REDIS_URL='redis://x'
        ):
            with mock.patch('sms.tasks.fan_out_message.delay',
                            side_effect=OSError('broker down')):
                with mock.patch('sms.outbound.dispatch') as inline:
                    outbound._dispatch_somewhere(9)
        inline.assert_called_once_with(9)


class SmsChecksTests(TestCase):
    """Misconfigurations that are silent at runtime must fail the deploy."""

    def ids(self, **overrides):
        from sms.checks import check_sms_configuration

        with override_settings(**overrides):
            return {problem.id for problem in check_sms_configuration(None)}

    def test_a_live_webhook_with_no_signing_keys_is_an_error(self):
        # The only authentication on the one public endpoint in the project.
        self.assertIn(
            'sms.E001',
            self.ids(TELNYX_MESSAGING_PROFILE_ID='p', TELNYX_PUBLIC_KEYS=[]),
        )

    def test_a_properly_configured_deployment_is_clean(self):
        self.assertEqual(
            self.ids(
                TELNYX_MESSAGING_PROFILE_ID='p',
                TELNYX_PUBLIC_KEYS=['k'],
                REDIS_URL='redis://x',
            ),
            set(),
        )

    def test_sending_on_with_no_provider_warns(self):
        self.assertIn(
            'sms.W001',
            self.ids(SMS_SENDING_ENABLED=True, TELNYX_MESSAGING_PROFILE_ID=''),
        )

    def test_per_process_budgets_warn(self):
        self.assertIn(
            'sms.W002',
            self.ids(TELNYX_MESSAGING_PROFILE_ID='p', TELNYX_PUBLIC_KEYS=['k'],
                     REDIS_URL=''),
        )

    def test_an_unlimited_budget_is_refused(self):
        self.assertIn('sms.E002', self.ids(SMS_MAX_SEGMENTS_PER_DAY=0))


@override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
class NumberProvisioningTests(TestCase):
    """The three rules a group's number obeys.

    1. Its country follows whoever created the group.
    2. It never changes.
    3. Deleting the group deletes it.

    A provider that always sells is faked at `sms.client._request`, the single
    seam every provider call goes through, so these stay tests of the rules
    rather than of the network. It has to be faked because nothing mints a
    stand-in number any more - with no provider configured a group simply has
    no number, which `NoProviderMeansNoNumberTests` pins separately.
    """

    def setUp(self):
        self._minted = 0
        patcher = mock.patch('sms.client._request', side_effect=self.fake_provider)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_provider(self, method, url, payload=None, **kwargs):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)

        # Checked before `phone_numbers`, which is a substring of it.
        if 'available_phone_numbers' in url:
            country = (query.get('filter[country_code]') or [''])[0]
            prefix = next(
                (p for p, c in settings.SMS_COUNTRY_BY_PREFIX.items() if c == country),
                None,
            )
            if prefix is None:
                # A country the provider does not sell in answers with an empty
                # list rather than an error, exactly as Telnyx does.
                return {'data': []}
            self._minted += 1
            return {'data': [{
                'phone_number': f'{prefix}9{self._minted:08d}',
                'features': [{'name': 'sms'}],
            }]}

        if method == 'POST' and 'number_orders' in url:
            return {'data': {'id': f'order-{self._minted}', 'status': 'success'}}

        if method == 'DELETE':
            return {}

        if 'phone_numbers' in url:
            wanted = (query.get('filter[phone_number]') or [''])[0]
            return {'data': [{'id': f'num-{wanted.lstrip("+")}', 'phone_number': wanted}]}

        raise AssertionError(f'unexpected provider call: {method} {url}')

    def make_user(self, name, phone=None, verified=True):
        user = User.objects.create_user(username=name, password='x')
        if phone:
            user.profile.phone_number = phone
            user.profile.phone_no_verified = verified
            user.profile.save()
        return user

    def create_group(self, creator, name='Family'):
        self.client.force_login(creator)
        conversation = Conversation.start_group(creator, name=name)
        provisioning.provision_for(conversation, creator)
        return conversation

    # --- rule 1: the creator's country -------------------------------------

    def test_a_philippine_creator_gets_a_philippine_number(self):
        yul = self.make_user('yul', phone='+639171111111')
        group = self.create_group(yul)

        number = GroupNumber.objects.get(conversation=group)
        self.assertEqual(number.country, 'PH')
        self.assertTrue(number.number.startswith('+63'))

    def test_a_us_creator_gets_a_us_number(self):
        john = self.make_user('john', phone='+14155552222')
        number = GroupNumber.objects.get(conversation=self.create_group(john))
        self.assertEqual(number.country, 'US')
        self.assertTrue(number.number.startswith('+1'))

    def test_an_unverified_number_buys_nothing_and_turns_sms_off(self):
        # An unverified number is a string somebody typed, not evidence of where
        # they are. Rule 1a: there is no default country to fall back to,
        # because a guess buys a number that can never be changed.
        eve = self.make_user('eve', phone='+639171111111', verified=False)

        group = self.create_group(eve)

        self.assertFalse(GroupNumber.objects.filter(conversation=group).exists())
        group.refresh_from_db()
        self.assertFalse(group.sms_delivery_enabled)
        self.assertFalse(group.sms_replies_enabled)

    def test_a_creator_with_no_number_at_all_still_gets_a_group(self):
        # A phone number is optional at registration, so this is a real case
        # and must not stop somebody creating a group. They get a group that
        # works in the app and says, in its own settings, that SMS is not on.
        nobody = self.make_user('nobody')

        group = self.create_group(nobody)

        self.assertTrue(Conversation.objects.filter(pk=group.pk).exists())
        self.assertFalse(GroupNumber.objects.filter(conversation=group).exists())
        group.refresh_from_db()
        self.assertFalse(group.sms_delivery_enabled)

    def test_a_verified_number_in_a_country_we_cannot_buy_in_turns_sms_off(self):
        # Same rule, reached the other way: the number is verified, but
        # SMS_COUNTRY_BY_PREFIX has no entry for +55, so there is no country to
        # buy in. Guessing here would be the same permanent mistake.
        bruno = self.make_user('bruno', phone='+5511999999999')

        group = self.create_group(bruno)

        self.assertFalse(GroupNumber.objects.filter(conversation=group).exists())
        group.refresh_from_db()
        self.assertFalse(group.sms_delivery_enabled)

    def test_verifying_a_number_later_lets_the_group_be_provisioned(self):
        # The switch-off is not a dead end. Once there is a country to buy in
        # and an admin turns delivery back on, the maintenance job finishes the
        # job that could not be done at creation.
        from sms import maintenance

        late = self.make_user('late')
        group = self.create_group(late)
        self.assertFalse(GroupNumber.objects.filter(conversation=group).exists())

        late.profile.phone_number = '+639171112222'
        late.profile.phone_no_verified = True
        late.profile.save()

        # Still off, so the job leaves it alone rather than spending money on a
        # group nobody has asked to switch back on.
        self.assertEqual(maintenance.provision_missing_numbers(), 0)
        self.assertFalse(GroupNumber.objects.filter(conversation=group).exists())

        group.sms_delivery_enabled = True
        group.save(update_fields=['sms_delivery_enabled'])

        self.assertEqual(maintenance.provision_missing_numbers(), 1)
        self.assertEqual(
            GroupNumber.objects.get(conversation=group).country, 'PH'
        )

    def test_every_group_gets_a_different_number(self):
        yul = self.make_user('yul', phone='+639171111111')
        first = self.create_group(yul, name='Family')
        second = self.create_group(yul, name='Work')
        self.assertNotEqual(
            GroupNumber.objects.get(conversation=first).number,
            GroupNumber.objects.get(conversation=second).number,
        )

    # --- rule 2: it never changes ------------------------------------------

    def test_provisioning_twice_keeps_the_first_number(self):
        yul = self.make_user('yul', phone='+639171111111')
        group = self.create_group(yul)
        original = GroupNumber.objects.get(conversation=group).number

        provisioning.provision_for(group, yul)

        self.assertEqual(GroupNumber.objects.filter(conversation=group).count(), 1)
        self.assertEqual(
            GroupNumber.objects.get(conversation=group).number, original
        )

    def test_the_api_exposes_the_number_but_never_accepts_one(self):
        yul = self.make_user('yul', phone='+639171111111')
        group = self.create_group(yul)
        number = GroupNumber.objects.get(conversation=group)

        client = APIClient()
        client.force_authenticate(yul)
        detail = client.get(reverse('chat:conversation-detail', args=[group.id]))
        # response.data is pre-render; the envelope is applied by the renderer.
        self.assertEqual(
            detail.json()['data']['sms_number'],
            {
                'number': number.number,
                'country': number.country,
                'status': GroupNumber.Status.ACTIVE,
                # Names who the lease is with, which is where a support
                # conversation about this number has to start.
                'provider': 'telnyx',
            },
        )

        # The lease's provider-side handle is never part of the response.
        self.assertNotIn('provider_number_id', detail.json()['data']['sms_number'])

        # And there is no writable form of it anywhere.
        response = client.patch(
            reverse('chat:conversation-settings', args=[group.id]),
            {'sms_number': '+14155559999'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            GroupNumber.objects.get(conversation=group).number, number.number
        )

    # --- rule 3: deleting the group deletes the number ----------------------

    def test_deleting_the_group_deletes_its_number(self):
        yul = self.make_user('yul', phone='+639171111111')
        group = self.create_group(yul)
        self.assertEqual(GroupNumber.objects.count(), 1)

        group.delete()

        self.assertEqual(GroupNumber.objects.count(), 0)

    def test_a_provider_failure_never_blocks_deleting_a_group(self):
        # A number nobody could release is worth an alarm, not a group that
        # cannot be deleted.
        yul = self.make_user('yul', phone='+639171111111')
        group = self.create_group(yul)

        with mock.patch('sms.provisioning.release_number',
                        side_effect=OSError('provider down')):
            group.delete()

        self.assertFalse(Conversation.objects.filter(pk=group.pk).exists())

    def test_deleting_a_direct_conversation_touches_no_numbers(self):
        yul = self.make_user('yul', phone='+639171111111')
        bob = self.make_user('bob', phone='+639172222222')
        group = self.create_group(yul)
        direct, _ = Conversation.start_direct(yul, bob)

        direct.delete()

        self.assertEqual(GroupNumber.objects.filter(conversation=group).count(), 1)


@override_settings(TELNYX_API_KEY='', TELNYX_MESSAGING_PROFILE_ID='')
class NoProviderMeansNoNumberTests(TestCase):
    """With no provider account, a group gets NO number - not a made-up one.

    There used to be a placeholder here: a well-formed, randomly generated
    number minted locally so that group creation worked end to end on a laptop.
    It was indistinguishable from a real number everywhere it was later read.
    The API served it, a client rendered it, and somebody was told to text a
    line that belongs to nobody. Null is the honest answer, and the only one a
    client can act on.
    """

    def make_group(self):
        user = User.objects.create_user(username='yul', password='x')
        user.profile.phone_number = '+639171111111'
        user.profile.phone_no_verified = True
        user.profile.save()
        conversation = Conversation.start_group(user, name='Family')
        provisioning.provision_for(conversation, user)
        return user, conversation

    def test_no_number_row_is_created(self):
        _, group = self.make_group()
        self.assertFalse(GroupNumber.objects.filter(conversation=group).exists())
        # And nothing was invented under any other status either.
        self.assertEqual(GroupNumber.objects.count(), 0)

    def test_the_group_itself_still_works(self):
        # The whole point of provisioning sitting outside the creating
        # transaction: no number is not a failed group.
        _, group = self.make_group()
        self.assertTrue(Conversation.objects.filter(pk=group.pk).exists())

    def test_the_api_says_null_rather_than_an_empty_string(self):
        user, group = self.make_group()
        client = APIClient()
        client.force_authenticate(user)
        detail = client.get(reverse('chat:conversation-detail', args=[group.id]))
        self.assertIsNone(detail.json()['data']['sms_number'])

    def test_delivery_stays_on_so_the_backfill_retries(self):
        # Distinct from rule 1a, which switches SMS off because the group can
        # NEVER have a number. A missing provider is temporary, and delivery
        # being left on is what `provision_missing_numbers` keys on.
        _, group = self.make_group()
        group.refresh_from_db()
        self.assertTrue(group.sms_delivery_enabled)

    def test_ordering_returns_none_instead_of_minting_one(self):
        self.assertIsNone(provisioning.order_number('PH'))


class CountryLookupTests(TestCase):
    """Which country an E.164 number belongs to."""

    def test_the_countries_we_serve(self):
        self.assertEqual(provisioning.country_for_number('+639171234567'), 'PH')
        self.assertEqual(provisioning.country_for_number('+14155551234'), 'US')
        self.assertEqual(provisioning.country_for_number('+441632960000'), 'GB')

    def test_an_unknown_country_is_none_rather_than_a_guess(self):
        # Brazil: not in the table, so no guess is made and the caller falls
        # back rather than buying in a country nobody chose.
        self.assertIsNone(provisioning.country_for_number('+5511999999999'))

    def test_junk_is_none(self):
        for value in ['', None, 'not a number', '09171234567']:
            self.assertIsNone(provisioning.country_for_number(value))


class AppMembersReceiveTextsTests(TestCase):
    """A member with an account and a verified number gets the text.

    Verifying a phone number at sign-up is what makes this possible, and it is
    the point of collecting it. Having the app installed does not mean somebody
    stops wanting the message on their phone - the app and SMS are two ways into
    one conversation.
    """

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        GroupNumber.objects.create(
            number='+14155551001', conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.sender = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

    def add_app_member(self, name, phone, verified=True):
        user = User.objects.create_user(username=name, password='x')
        user.profile.phone_number = phone
        user.profile.phone_no_verified = verified
        user.profile.save()
        self.group.add_participant(user)
        return user

    def deliveries(self):
        message = create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=self.sender,
        )
        return {d.participant.user_id: d for d in outbound.fan_out(message)}

    def test_an_app_member_with_a_verified_number_is_texted(self):
        bob = self.add_app_member('bob', '+639171111111')
        row = self.deliveries()[bob.pk]
        self.assertEqual(row.status, SmsDelivery.Status.SENT)

    def test_they_are_on_by_default_without_anybody_opting_them_in(self):
        bob = self.add_app_member('bob', '+639171111111')
        membership_row = Participant.objects.get(conversation=self.group, user=bob)
        self.assertTrue(membership_row.sms_enabled)

    def test_an_unverified_number_is_not_texted(self):
        # Verification is what makes the number theirs. Texting an unverified
        # one sends this group's messages to whoever really owns it.
        eve = self.add_app_member('eve', '+639172222222', verified=False)
        row = self.deliveries()[eve.pk]
        self.assertEqual(row.status, SmsDelivery.Status.SKIPPED)
        self.assertEqual(row.reason, 'no verified number')

    def test_a_member_with_no_number_is_skipped_not_broken(self):
        nobody = User.objects.create_user(username='nobody', password='x')
        self.group.add_participant(nobody)
        row = self.deliveries()[nobody.pk]
        self.assertEqual(row.status, SmsDelivery.Status.SKIPPED)

    def test_a_member_can_turn_this_group_to_app_only(self):
        bob = self.add_app_member('bob', '+639171111111')
        row = Participant.objects.get(conversation=self.group, user=bob)
        membership.set_delivery(row, sms_enabled=False)

        self.assertEqual(
            self.deliveries()[bob.pk].reason, 'sms disabled for this group'
        )

    def test_turning_texts_off_does_not_silence_the_app_too(self):
        # Two switches answering different questions.
        bob = self.add_app_member('bob', '+639171111111')
        row = Participant.objects.get(conversation=self.group, user=bob)
        membership.set_delivery(row, sms_enabled=False)
        row.refresh_from_db()
        self.assertFalse(row.muted)

    def test_setting_one_switch_leaves_the_other_alone(self):
        bob = self.add_app_member('bob', '+639171111111')
        row = Participant.objects.get(conversation=self.group, user=bob)
        membership.set_delivery(row, sms_enabled=False)
        membership.set_delivery(row, muted=True)
        row.refresh_from_db()
        self.assertFalse(row.sms_enabled)
        self.assertTrue(row.muted)

    def test_account_wide_off_beats_the_per_group_setting(self):
        bob = self.add_app_member('bob', '+639171111111')
        bob.profile.sms_enabled = False
        bob.profile.save(update_fields=['sms_enabled'])
        self.assertEqual(
            self.deliveries()[bob.pk].reason, 'sms disabled account-wide'
        )


class BuyingANumberTests(TestCase):
    """Ordering a number from the provider.

    Every trap here is one that costs real money or silently keeps costing it,
    so each has its own test. The HTTP layer is mocked at `sms.client._request`,
    the single seam every provider call goes through.
    """

    SEARCH = {'data': [
        {'phone_number': '+14155550001', 'features': [{'name': 'sms'}, {'name': 'voice'}]},
        {'phone_number': '+14155550002', 'features': [{'name': 'sms'}]},
    ]}
    ORDER_OK = {'data': {'id': 'order-1', 'status': 'success'}}
    ORDER_PENDING = {'data': {'id': 'order-1', 'status': 'pending'}}
    INVENTORY = {'data': [{'id': '1234567890', 'phone_number': '+14155550001'}]}

    def setUp(self):
        cache.clear()

    def route(self, responses):
        """Answer each call by (method, url-fragment), in order of specificity."""
        def fake(method, url, **kwargs):
            for (m, fragment), value in responses.items():
                if m == method and fragment in url:
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise AssertionError(f'unexpected call: {method} {url}')
        return fake

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_the_happy_path_searches_then_orders_then_confirms(self):
        calls = []

        def fake(method, url, **kwargs):
            calls.append((method, url.split('/v2/')[1].split('?')[0]))
            if 'available_phone_numbers' in url:
                return self.SEARCH
            if method == 'POST' and 'number_orders' in url:
                return self.ORDER_OK
            if 'phone_numbers' in url:
                return self.INVENTORY
            raise AssertionError(url)

        with mock.patch('sms.client._request', side_effect=fake):
            result = provisioning.order_number('US')

        self.assertEqual(result['number'], '+14155550001')
        # The releasable id comes from the INVENTORY lookup, never from the
        # order response - the order carries a different kind of id entirely.
        self.assertEqual(result['provider_number_id'], '1234567890')
        # A search must precede the order, or the provider refuses it.
        self.assertEqual(calls[0][0], 'GET')
        self.assertIn('available_phone_numbers', calls[0][1])

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_the_messaging_profile_goes_at_the_top_level_of_the_order(self):
        # Nested inside phone_numbers[] the order still succeeds, and the number
        # arrives attached to no messaging profile - it can neither send nor
        # receive, and nothing says why.
        captured = {}

        def fake(method, url, payload=None, **kwargs):
            if 'available_phone_numbers' in url:
                return self.SEARCH
            if method == 'POST':
                captured.update(payload or {})
                return self.ORDER_OK
            return self.INVENTORY

        with mock.patch('sms.client._request', side_effect=fake):
            provisioning.order_number('US')

        self.assertEqual(captured.get('messaging_profile_id'), 'p')
        self.assertNotIn('messaging_profile_id', captured['phone_numbers'][0])

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p',
                       SMS_NUMBER_ORDER_POLL_SECONDS=0)
    def test_a_pending_order_is_polled_until_it_settles(self):
        # A 200 means accepted, not owned. Writing the number down on the 200
        # hands a group a number that is not ours yet.
        states = [self.ORDER_PENDING, {'data': {'id': 'order-1', 'status': 'success'}}]

        def fake(method, url, **kwargs):
            if 'available_phone_numbers' in url:
                return self.SEARCH
            if method == 'POST':
                return self.ORDER_PENDING
            if 'number_orders/' in url:
                return states.pop() if len(states) > 1 else states[0]
            return self.INVENTORY

        with mock.patch('sms.client._request', side_effect=fake):
            result = provisioning.order_number('US')
        self.assertEqual(result['number'], '+14155550001')

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p',
                       SMS_NUMBER_ORDER_POLLS=1, SMS_NUMBER_ORDER_POLL_SECONDS=0)
    def test_an_order_stuck_pending_is_not_treated_as_owned(self):
        def fake(method, url, **kwargs):
            if 'available_phone_numbers' in url:
                return self.SEARCH
            if method == 'POST':
                return self.ORDER_PENDING
            return self.ORDER_PENDING

        with mock.patch('sms.client._request', side_effect=fake):
            with self.assertLogs('sms.provisioning', level='ERROR'):
                self.assertIsNone(provisioning.order_number('US'))

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_a_number_taken_in_the_gap_moves_to_the_next_candidate(self):
        # A search reserves nothing, so this is the normal case under load.
        ordered = []

        def fake(method, url, payload=None, **kwargs):
            if 'available_phone_numbers' in url:
                return self.SEARCH
            if method == 'POST':
                number = payload['phone_numbers'][0]['phone_number']
                ordered.append(number)
                if number == '+14155550001':
                    raise sms.client.SmsError('taken')
                return self.ORDER_OK
            return {'data': [{'id': '999', 'phone_number': '+14155550002'}]}

        with mock.patch('sms.client._request', side_effect=fake):
            result = provisioning.order_number('US')

        self.assertEqual(ordered, ['+14155550001', '+14155550002'])
        self.assertEqual(result['number'], '+14155550002')

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_a_country_with_no_inventory_gives_up_rather_than_buying_wrongly(self):
        # An unsupported country looks exactly like this: the country filter has
        # no enum, so it returns an empty list rather than an error.
        with mock.patch('sms.client._request', return_value={'data': []}):
            with self.assertLogs('sms.provisioning', level='ERROR'):
                self.assertIsNone(provisioning.order_number('PH'))

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_numbers_without_sms_are_never_bought(self):
        # features is a list of OBJECTS. `'sms' in features` is False for every
        # number ever written, and looks exactly like "none available".
        voice_only = {'data': [
            {'phone_number': '+14155550003', 'features': [{'name': 'voice'}]}
        ]}
        with mock.patch('sms.client._request', return_value=voice_only):
            with self.assertLogs('sms.provisioning', level='ERROR'):
                self.assertIsNone(provisioning.order_number('US'))

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_releasing_uses_the_inventory_id_and_stops_the_billing(self):
        row = GroupNumber(number='+14155550001', provider_number_id='1234567890')
        seen = {}

        def fake(method, url, **kwargs):
            seen['method'], seen['url'] = method, url
            return {}

        with mock.patch('sms.client._request', side_effect=fake):
            self.assertTrue(provisioning.release_number(row))

        self.assertEqual(seen['method'], 'DELETE')
        self.assertTrue(seen['url'].endswith('/phone_numbers/1234567890'))

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_a_failed_release_is_loud_because_the_charge_continues(self):
        row = GroupNumber(number='+14155550001', provider_number_id='1234567890')
        with mock.patch('sms.client._request',
                        side_effect=sms.client.SmsError('provider down')):
            with self.assertLogs('sms.provisioning', level='ERROR'):
                self.assertFalse(provisioning.release_number(row))


class CountryFallbackTests(TestCase):
    """What the fallback ladder does when it is deliberately switched on.

    It is EMPTY by default (rule 1b): a group that cannot get a number in its
    own country gets none, because a consolation number abroad is permanent
    (rule 2) and bills every member international rates. Every test here sets
    the ladder explicitly, so none of them describes the default.
    """

    def setUp(self):
        # The ladder checks for a provider once, before trying any country. No
        # account is configured under test, so without this it returns before
        # attempting anything and every assertion here reads as "no countries
        # tried" rather than as the ordering behaviour being tested.
        patcher = mock.patch(
            'sms.provisioning.messaging_is_configured', return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @override_settings(SMS_FALLBACK_COUNTRIES=[])
    def test_by_default_there_is_no_second_choice(self):
        """Rule 1b: no number is a valid answer, a foreign number is not."""
        attempted = []

        def fake(country):
            attempted.append(country)
            return None

        with mock.patch('sms.provisioning.order_number', side_effect=fake):
            order, country = provisioning._order_with_fallback('PH')

        self.assertEqual(attempted, ['PH'])
        self.assertIsNone(order)
        self.assertEqual(country, 'PH')

    @override_settings(SMS_FALLBACK_COUNTRIES=['US'])
    def test_it_falls_back_when_the_creators_country_has_nothing(self):
        attempted = []

        def fake(country):
            attempted.append(country)
            if country == 'PH':
                return None
            return {'number': '+14155550001', 'provider': 'telnyx',
                    'provider_number_id': '9'}

        with mock.patch('sms.provisioning.order_number', side_effect=fake):
            with self.assertLogs('sms.provisioning', level='WARNING'):
                order, country = provisioning._order_with_fallback('PH')

        self.assertEqual(attempted, ['PH', 'US'])
        self.assertEqual(country, 'US')
        self.assertEqual(order['number'], '+14155550001')

    @override_settings(SMS_FALLBACK_COUNTRIES=['US', 'GB'])
    def test_the_creators_country_is_still_tried_first(self):
        attempted = []

        def fake(country):
            attempted.append(country)
            return {'number': '+639171111111', 'provider': '', 'provider_number_id': ''}

        with mock.patch('sms.provisioning.order_number', side_effect=fake):
            _, country = provisioning._order_with_fallback('PH')

        self.assertEqual(attempted, ['PH'])
        self.assertEqual(country, 'PH')

    @override_settings(SMS_FALLBACK_COUNTRIES=['US'])
    def test_a_country_is_never_tried_twice(self):
        attempted = []

        def fake(country):
            attempted.append(country)
            return None

        with mock.patch('sms.provisioning.order_number', side_effect=fake):
            order, _ = provisioning._order_with_fallback('US')

        self.assertEqual(attempted, ['US'])
        self.assertIsNone(order)

    @override_settings(SMS_FALLBACK_COUNTRIES=['US'])
    def test_a_group_still_gets_created_when_no_country_works(self):
        # A VERIFIED number, so this exercises "every order failed" rather than
        # rule 1a's "there is no country to order in" - which returns earlier
        # and is a decision, not an error.
        alice = User.objects.create_user(username='alice', password='x')
        alice.profile.phone_number = '+14155552222'
        alice.profile.phone_no_verified = True
        alice.profile.save()
        group = Conversation.start_group(alice, name='Family')

        with mock.patch('sms.provisioning.order_number', return_value=None):
            with self.assertLogs('sms.provisioning', level='ERROR'):
                self.assertIsNone(provisioning.provision_for(group, alice))

        self.assertTrue(Conversation.objects.filter(pk=group.pk).exists())


class SmsSettingsGateTests(TestCase):
    """Rule 1c: a number is only bought for a group that actually uses SMS.

    Both legs off is a deliberate choice at creation - the two Features
    checkboxes are unticked - and buying anyway would lease a number, billed
    every month, for a channel the creator switched off before the group
    existed.
    """

    def setUp(self):
        self.creator = User.objects.create_user(username='yul', password='x')
        self.creator.profile.phone_number = '+14155550000'
        self.creator.profile.phone_no_verified = True
        self.creator.profile.save()
        self.bought = {'number': '+14155550123', 'provider': 'telnyx',
                       'provider_number_id': 'num-1'}

    def a_group(self, *, delivery, replies):
        group = Conversation.start_group(self.creator, name='Family')
        group.sms_delivery_enabled = delivery
        group.sms_replies_enabled = replies
        group.save(update_fields=['sms_delivery_enabled', 'sms_replies_enabled'])
        return group

    def provision(self, group):
        with mock.patch('sms.provisioning.messaging_is_configured',
                        return_value=True):
            with mock.patch('sms.provisioning.order_number',
                            return_value=self.bought) as order:
                result = provisioning.provision_for(group, self.creator)
        return result, order

    def test_both_off_buys_nothing(self):
        group = self.a_group(delivery=False, replies=False)
        result, order = self.provision(group)

        self.assertIsNone(result)
        order.assert_not_called()
        self.assertEqual(GroupNumber.objects.count(), 0)

    def test_delivery_alone_is_enough(self):
        group = self.a_group(delivery=True, replies=False)
        result, order = self.provision(group)

        self.assertIsNotNone(result)
        self.assertEqual(result.number, '+14155550123')
        order.assert_called_once()

    def test_replies_alone_is_enough(self):
        """A group people text INTO still needs a number to text."""
        group = self.a_group(delivery=False, replies=True)
        result, order = self.provision(group)

        self.assertIsNotNone(result)
        order.assert_called_once()

    def test_both_on_buys_one(self):
        group = self.a_group(delivery=True, replies=True)
        result, _ = self.provision(group)
        self.assertIsNotNone(result)

    def test_the_settings_are_left_exactly_as_they_were(self):
        # Not _disable_sms: they already say this, and rewriting them would be
        # a pointless UPDATE on a row the conversation list is ordered by.
        group = self.a_group(delivery=False, replies=False)
        before = group.updated_at
        self.provision(group)

        group.refresh_from_db()
        self.assertFalse(group.sms_delivery_enabled)
        self.assertFalse(group.sms_replies_enabled)
        self.assertEqual(group.updated_at, before)

    def test_an_available_spare_is_not_claimed_either(self):
        # The gate is checked before the spare lookup, so a group that wants no
        # number cannot consume one somebody else is holding.
        GroupNumber.objects.create(
            number='+14155559999', status=GroupNumber.Status.AVAILABLE,
            country='US', provider='telnyx',
        )
        group = self.a_group(delivery=False, replies=False)
        result, _ = self.provision(group)

        self.assertIsNone(result)
        spare = GroupNumber.objects.get(number='+14155559999')
        self.assertEqual(spare.status, GroupNumber.Status.AVAILABLE)
        self.assertIsNone(spare.conversation)

    def test_uses_sms_reads_both_legs(self):
        self.assertFalse(provisioning.uses_sms(
            self.a_group(delivery=False, replies=False)))
        self.assertTrue(provisioning.uses_sms(
            self.a_group(delivery=True, replies=False)))
        self.assertTrue(provisioning.uses_sms(
            self.a_group(delivery=False, replies=True)))

    def test_turning_a_leg_on_later_is_picked_up_by_the_backfill(self):
        """Rule 1c is reversible - that is what makes it safe to default off."""
        group = self.a_group(delivery=False, replies=False)
        self.provision(group)
        self.assertEqual(GroupNumber.objects.count(), 0)

        group.sms_replies_enabled = True
        group.save(update_fields=['sms_replies_enabled'])

        with mock.patch('sms.provisioning.messaging_is_configured',
                        return_value=True):
            with mock.patch('sms.provisioning.order_number',
                            return_value=self.bought):
                self.assertEqual(maintenance.provision_missing_numbers(), 1)

        self.assertTrue(GroupNumber.objects.filter(conversation=group).exists())

    def test_the_backfill_leaves_a_both_off_group_alone(self):
        self.a_group(delivery=False, replies=False)

        with mock.patch('sms.provisioning.messaging_is_configured',
                        return_value=True):
            with mock.patch('sms.provisioning.order_number',
                            return_value=self.bought) as order:
                self.assertEqual(maintenance.provision_missing_numbers(), 0)

        order.assert_not_called()
        self.assertEqual(GroupNumber.objects.count(), 0)


class ForcedCountryTests(TestCase):
    """`SMS_FORCE_COUNTRY`: buy everything in one country, whoever asks.

    The reason it exists is measurable rather than theoretical. Telnyx sells no
    SMS-capable number in PH, GB, AU or SG - a search for one answers HTTP 400
    'No coverage found', because the numbers on offer there are voice/fax only.
    US and CA are the only two that can be bought and texted from, so without
    this the feature cannot be exercised from a +63 account at all.

    It is set on staging and empty in production, where rule 1 decides.
    """

    def a_creator(self, name='yul', phone='+639171111111', verified=True):
        user = User.objects.create_user(username=name, password='x')
        if phone:
            user.profile.phone_number = phone
            user.profile.phone_no_verified = verified
            user.profile.save()
        return user

    @override_settings(SMS_FORCE_COUNTRY='US')
    def test_it_overrides_the_creators_own_country(self):
        self.assertEqual(provisioning.country_for(self.a_creator()), 'US')

    @override_settings(SMS_FORCE_COUNTRY='US')
    def test_it_overrides_rule_1a_as_well(self):
        # No verified number normally means no country and no SMS. An explicit
        # operator override is not the guess rule 1a exists to prevent, and a
        # test account without a verified number is the common case on staging.
        nobody = self.a_creator(phone=None)
        self.assertEqual(provisioning.country_for(nobody), 'US')

    @override_settings(SMS_FORCE_COUNTRY='')
    def test_empty_leaves_rules_1_and_1a_alone(self):
        self.assertEqual(provisioning.country_for(self.a_creator()), 'PH')
        self.assertIsNone(
            provisioning.country_for(self.a_creator(name='nobody', phone=None))
        )

    @override_settings(SMS_FORCE_COUNTRY='US', SMS_FALLBACK_COUNTRIES=[])
    def test_a_philippine_creator_gets_a_us_number_end_to_end(self):
        creator = self.a_creator()
        group = Conversation.start_group(creator, name='Family')

        bought = {'number': '+14155550123', 'provider': 'telnyx',
                  'provider_number_id': 'num-1'}
        with mock.patch('sms.provisioning.messaging_is_configured',
                        return_value=True):
            with mock.patch('sms.provisioning.order_number',
                            return_value=bought) as order:
                row = provisioning.provision_for(group, creator)

        order.assert_called_once_with('US')
        self.assertEqual(row.country, 'US')
        self.assertEqual(row.number, '+14155550123')

    @override_settings(SMS_FORCE_COUNTRY='US', SMS_FALLBACK_COUNTRIES=[])
    def test_no_number_available_leaves_the_group_with_null(self):
        """Rule 1b. Not a number somewhere else, and not a fabricated one."""
        creator = self.a_creator()
        group = Conversation.start_group(creator, name='Family')

        with mock.patch('sms.provisioning.messaging_is_configured',
                        return_value=True):
            with mock.patch('sms.provisioning.order_number', return_value=None):
                with self.assertLogs('sms.provisioning', level='ERROR'):
                    self.assertIsNone(provisioning.provision_for(group, creator))

        self.assertEqual(GroupNumber.objects.count(), 0)
        group.refresh_from_db()
        # Left ON deliberately: nothing here says the group can NEVER have a
        # number, and delivery being on is what the backfill keys on.
        self.assertTrue(group.sms_delivery_enabled)


class SpendCapsTests(TestCase):
    """Bounds on what one message, or one guessing number, can cost."""

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.join_mode = Conversation.JoinMode.CODE
        self.group.save(update_fields=['join_mode'])
        GroupNumber.objects.create(
            number='+14155551001', conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.sender = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

    @override_settings(SMS_MAX_RECIPIENTS_PER_MESSAGE=2)
    def test_a_group_too_large_to_text_is_skipped_not_billed(self):
        for i in range(3):
            row = self.group.add_phone_participant(
                f'+63917000000{i}', display_name=f'M{i}'
            )
            row.sms_consent = Participant.Consent.GRANTED
            row.save(update_fields=['sms_consent'])

        message = create_message(
            conversation=self.group, sender=self.alice, text='hi',
            participant=self.sender,
        )
        with self.assertLogs('sms.outbound', level='ERROR'):
            rows = outbound.fan_out(message)

        self.assertTrue(all(r.status == SmsDelivery.Status.SKIPPED for r in rows))
        self.assertTrue(all(r.reason == 'group is too large to text' for r in rows))

    @override_settings(SMS_MAX_JOIN_ATTEMPTS_PER_HOUR=2)
    def test_a_number_cannot_guess_join_codes_without_limit(self):
        first = inbound.handle_inbound(self.group, '+639179999999', 'JOIN WRONG1')
        second = inbound.handle_inbound(self.group, '+639179999999', 'JOIN WRONG2')
        third = inbound.handle_inbound(self.group, '+639179999999', 'JOIN WRONG3')

        self.assertEqual(first.action, 'refused')
        self.assertEqual(second.action, 'refused')
        self.assertEqual(third.action, 'refused_rate_limited')
        # Silence: telling them they are guessing too fast is itself a hint.
        self.assertIsNone(third.reply)

    @override_settings(SMS_MAX_JOIN_ATTEMPTS_PER_HOUR=2)
    def test_the_cap_is_per_number_not_global(self):
        for _ in range(2):
            inbound.handle_inbound(self.group, '+639179999999', 'JOIN WRONG')

        other = inbound.handle_inbound(
            self.group, '+639178888888', f'JOIN {self.group.join_code}'
        )
        self.assertEqual(other.action, 'joined')


class InviteAndNotifyTests(TransactionTestCase):
    """Inviting somebody by number, and telling people what happened.

    TransactionTestCase, because every notice here is queued with
    `transaction.on_commit` - deliberately, so nobody is texted about a row that
    then rolls back. A plain TestCase wraps each test in a transaction it never
    commits, so none of them would ever fire and all of this would look like it
    was not wired up at all.
    """

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        GroupNumber.objects.create(
            number='+14155551001', conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.number = '+639171111111'

    def sent(self):
        return list(
            SmsDelivery.objects.filter(
                direction=SmsDelivery.Direction.OUTBOUND,
                status=SmsDelivery.Status.SENT,
            )
        )

    # --- inviting -----------------------------------------------------------

    def test_an_admin_can_invite_somebody_who_has_no_account(self):
        # Without this, Invite Only is a mode with no way in at all.
        self.group.join_mode = Conversation.JoinMode.INVITE
        self.group.save(update_fields=['join_mode'])

        row = membership.invite_by_phone(self.group, self.number, 'Mom', self.alice)

        self.assertEqual(row.display_label, 'Mom')
        self.assertEqual(row.sms_consent, Participant.Consent.PENDING)
        self.assertTrue(row.consent_text)

    def test_the_invitation_is_texted_and_logged(self):
        membership.invite_by_phone(self.group, self.number, 'Mom', self.alice)
        rows = self.sent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].counterpart_last4, '1111')

    def test_nothing_from_the_group_reaches_them_until_they_reply_yes(self):
        # Somebody else putting your number in a group is not you agreeing to
        # be texted.
        row = membership.invite_by_phone(self.group, self.number, 'Mom', self.alice)
        sender = Participant.objects.get(conversation=self.group, user=self.alice)
        message = create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=sender,
        )
        # Read the log rather than fan_out's return: create_message already
        # scheduled the fan-out on commit, so calling it again correctly does
        # nothing (it is idempotent) and returns an empty list.
        delivery = SmsDelivery.objects.get(message=message, participant=row)
        self.assertEqual(delivery.reason, 'consent not granted')

    def test_replying_yes_opens_the_gate(self):
        row = membership.invite_by_phone(self.group, self.number, 'Mom', self.alice)
        inbound.handle_inbound(self.group, self.number, 'YES')
        row.refresh_from_db()
        self.assertEqual(row.sms_consent, Participant.Consent.GRANTED)

    def test_a_blocked_number_cannot_be_invited_back(self):
        row = membership.invite_by_phone(self.group, self.number, 'Mom', self.alice)
        membership.remove(row, self.alice, block=True)
        with self.assertRaises(BlockedFromConversation):
            membership.invite_by_phone(self.group, self.number, 'Mom', self.alice)

    def test_a_plain_member_cannot_invite(self):
        bob = User.objects.create_user(username='bob', password='x')
        self.group.add_participant(bob)
        with self.assertRaises(membership.JoinRefused):
            membership.invite_by_phone(self.group, self.number, 'Mom', bob)

    def test_re_inviting_somebody_who_already_agreed_does_not_reset_them(self):
        membership.invite_by_phone(self.group, self.number, 'Mom', self.alice)
        inbound.handle_inbound(self.group, self.number, 'YES')

        row = membership.invite_by_phone(self.group, self.number, 'Mom', self.alice)
        self.assertEqual(row.sms_consent, Participant.Consent.GRANTED)

    # --- being told what happened -------------------------------------------

    def test_an_approved_joiner_is_told_they_are_in(self):
        self.group.join_mode = Conversation.JoinMode.APPROVAL
        self.group.save(update_fields=['join_mode'])
        result = inbound.handle_inbound(self.group, self.number, 'JOIN')
        before = len(self.sent())

        membership.approve(result.participant, self.alice)

        after = self.sent()
        self.assertEqual(len(after), before + 1)
        self.assertIn('1111', after[-1].counterpart_last4)

    def test_a_declined_joiner_is_told_too(self):
        # Otherwise they wait forever on an answer that already happened.
        self.group.join_mode = Conversation.JoinMode.APPROVAL
        self.group.save(update_fields=['join_mode'])
        result = inbound.handle_inbound(self.group, self.number, 'JOIN')
        before = len(self.sent())

        membership.decline(result.participant, self.alice)

        self.assertEqual(len(self.sent()), before + 1)

    def test_an_account_member_is_not_texted_about_their_own_membership(self):
        # They see it in the app; texting them is a charge for something they
        # already know.
        self.group.join_mode = Conversation.JoinMode.APPROVAL
        self.group.save(update_fields=['join_mode'])
        bob = User.objects.create_user(username='bob', password='x')
        pending = membership.request_to_join(self.group, user=bob)
        before = len(self.sent())

        membership.approve(pending, self.alice)

        self.assertEqual(len(self.sent()), before)

    def test_admins_are_told_that_somebody_is_waiting(self):
        self.group.join_mode = Conversation.JoinMode.APPROVAL
        self.group.save(update_fields=['join_mode'])
        events = []

        with mock.patch('chat.services.send_to_group',
                        side_effect=lambda g, p: events.append((g, p))):
            inbound.handle_inbound(self.group, self.number, 'JOIN')

        requests = [p for _, p in events if p.get('type') == 'conversation.request']
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['conversation_id'], self.group.pk)


class LosingThingsSilentlyTests(TransactionTestCase):
    """Five ways the SMS layer used to lose money, messages or deliveries.

    Every one of these passed the whole test suite before it was found, which is
    why each now has a test naming exactly what was lost.
    """

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.number = GroupNumber.objects.create(
            number='+14155551001', conversation=self.group,
            status=GroupNumber.Status.ACTIVE, provider='telnyx',
            provider_number_id='num-1',
        )
        self.sender = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

    def add_member(self, number, name):
        row = self.group.add_phone_participant(number, display_name=name)
        row.sms_consent = Participant.Consent.GRANTED
        row.save(update_fields=['sms_consent'])
        return row

    # --- 1. a failed release used to destroy the record --------------------

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_a_number_we_could_not_release_is_kept_not_forgotten(self):
        # Deleting the row here is how a number ends up billed every month with
        # nothing in the system pointing at it.
        with mock.patch('sms.client._request',
                        side_effect=sms.client.SmsError('provider down')):
            with self.assertLogs('sms.provisioning', level='ERROR'):
                self.group.delete()

        kept = GroupNumber.objects.get(number='+14155551001')
        self.assertEqual(kept.status, GroupNumber.Status.RELEASED)
        self.assertIsNone(kept.conversation_id)

    @override_settings(TELNYX_API_KEY='k', TELNYX_MESSAGING_PROFILE_ID='p')
    def test_a_successful_release_does_delete_the_record(self):
        with mock.patch('sms.client._request', return_value={}):
            self.group.delete()
        self.assertEqual(GroupNumber.objects.count(), 0)

    # --- 2. an interrupted fan-out used to never resume ---------------------

    def test_a_fan_out_that_died_part_way_finishes_on_the_next_run(self):
        first = self.add_member('+639171111111', 'One')
        second = self.add_member('+639172222222', 'Two')

        message = create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=self.sender,
        )
        # Simulate the first attempt having reached only one of the two.
        SmsDelivery.objects.filter(participant=second).delete()

        outbound.fan_out(message)

        self.assertTrue(
            SmsDelivery.objects.filter(message=message, participant=second).exists()
        )

    def test_nobody_is_texted_twice_by_a_resumed_fan_out(self):
        member = self.add_member('+639171111111', 'One')
        message = create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=self.sender,
        )
        outbound.fan_out(message)
        outbound.fan_out(message)

        self.assertEqual(
            SmsDelivery.objects.filter(message=message, participant=member).count(), 1
        )

    # --- 3. a failed webhook used to swallow the message --------------------

    @override_settings(TELNYX_PUBLIC_KEYS=[])
    def test_a_webhook_that_fails_asks_for_a_retry_instead_of_losing_it(self):
        client = APIClient()
        body = {'data': {'id': 'evt_1', 'event_type': 'message.received',
                         'payload': {'from': {'phone_number': '+639171111111'},
                                     'to': [{'phone_number': '+14155551001'}],
                                     'text': 'hello'}}}

        with mock.patch('sms.inbound.handle_inbound',
                        side_effect=RuntimeError('boom')):
            first = client.post(reverse('sms:inbound-webhook'), body, format='json')

        self.assertEqual(first.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        # And the claim is gone, so the provider's retry can actually work.
        self.assertFalse(SmsWebhookEvent.objects.filter(provider_event_id='evt_1').exists())

        second = client.post(reverse('sms:inbound-webhook'), body, format='json')
        self.assertEqual(second.status_code, status.HTTP_200_OK)

    # --- 4. replies used to bypass every guard ------------------------------

    def test_a_reply_is_logged_and_counted_like_any_other_message(self):
        outbound.send_reply(self.group, self.number, '+639179999999', 'LoopD: help')
        row = SmsDelivery.objects.get(participant__isnull=True, message__isnull=True)
        self.assertEqual(row.status, SmsDelivery.Status.SENT)
        self.assertEqual(row.counterpart_last4, '9999')

    def test_a_reply_obeys_the_kill_switch(self):
        SmsSettings.objects.create(singleton=True, sending_enabled=False)
        row = outbound.send_reply(self.group, self.number, '+639179999999', 'hi')
        self.assertEqual(row.status, SmsDelivery.Status.SKIPPED)
        self.assertEqual(row.reason, 'sending is switched off')

    @override_settings(SMS_MAX_SEGMENTS_PER_DAY=0)
    def test_a_reply_obeys_the_spend_ceiling(self):
        row = outbound.send_reply(self.group, self.number, '+639179999999', 'hi')
        self.assertEqual(row.status, SmsDelivery.Status.SKIPPED)
        self.assertIn('budget', row.reason)

    # --- 5. a fan-out failure used to 500 a delivered message ---------------

    def test_a_broken_fan_out_does_not_fail_the_send(self):
        # The message is committed and already broadcast to every app member.
        # Telling the sender it failed is worse than useless.
        self.add_member('+639171111111', 'One')
        client = APIClient()
        client.force_authenticate(self.alice)

        with mock.patch('sms.outbound.fan_out',
                        side_effect=RuntimeError('provider exploded')):
            with self.assertLogs('sms.outbound', level='ERROR'):
                response = client.post(
                    reverse('chat:conversation-messages', args=[self.group.id]),
                    {'text': 'Dinner at 7'},
                )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Message.objects.filter(text='Dinner at 7').exists())


class MaintenanceTests(TransactionTestCase):
    """The jobs that keep things honest while nobody is watching."""

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.alice.profile.phone_number = '+14155550000'
        self.alice.profile.phone_no_verified = True
        self.alice.profile.save()
        self.group = Conversation.start_group(self.alice, name='Family')
        GroupNumber.objects.create(
            number='+14155551001', conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.mom = self.group.add_phone_participant('+639171111111', display_name='Mom')
        self.mom.sms_consent = Participant.Consent.GRANTED
        self.mom.save(update_fields=['sms_consent'])
        self.sender = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

    def a_failed_delivery(self, reason='carrier hiccup', attempts=0):
        message = create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=self.sender,
        )
        row = SmsDelivery.objects.get(message=message, participant=self.mom)
        row.status = SmsDelivery.Status.FAILED
        row.reason = reason
        row.attempts = attempts
        row.save(update_fields=['status', 'reason', 'attempts'])
        return row

    # --- retry --------------------------------------------------------------

    def test_a_failed_delivery_is_tried_again(self):
        row = self.a_failed_delivery()
        self.assertEqual(maintenance.retry_failed_deliveries(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, SmsDelivery.Status.SENT)
        self.assertEqual(row.attempts, 1)

    def test_retrying_stops_after_a_few_goes(self):
        # A disconnected number fails the same way forever; retrying it is a
        # standing charge for a message that will never arrive.
        row = self.a_failed_delivery(attempts=settings.SMS_MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(maintenance.retry_failed_deliveries(), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, SmsDelivery.Status.FAILED)

    def test_a_deliberate_skip_is_never_retried(self):
        # A skip is a decision - muted, opted out - not a failure. Re-deciding
        # it every five minutes would eventually text somebody who said no.
        row = self.a_failed_delivery()
        row.status = SmsDelivery.Status.SKIPPED
        row.reason = 'opted out'
        row.save(update_fields=['status', 'reason'])

        self.assertEqual(maintenance.retry_failed_deliveries(), 0)

    def test_a_permanent_reason_is_never_retried(self):
        self.a_failed_delivery(reason='no verified number')
        self.assertEqual(maintenance.retry_failed_deliveries(), 0)

    def test_an_old_failure_is_left_alone(self):
        # Nobody wants yesterday's "dinner at 7".
        row = self.a_failed_delivery()
        SmsDelivery.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        self.assertEqual(maintenance.retry_failed_deliveries(), 0)

    # --- stalled ------------------------------------------------------------

    def test_a_row_left_sending_by_a_dead_worker_is_rescued(self):
        # Not FAILED so nothing retries it, not SENT so nobody knows it never
        # arrived. It would sit there forever.
        row = self.a_failed_delivery()
        SmsDelivery.objects.filter(pk=row.pk).update(
            status=SmsDelivery.Status.SENDING,
            updated_at=timezone.now() - timedelta(hours=1),
        )
        with self.assertLogs('sms.maintenance', level='WARNING'):
            self.assertEqual(maintenance.requeue_stalled_deliveries(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, SmsDelivery.Status.FAILED)

    def test_a_send_still_in_flight_is_not_disturbed(self):
        row = self.a_failed_delivery()
        SmsDelivery.objects.filter(pk=row.pk).update(status=SmsDelivery.Status.SENDING)
        self.assertEqual(maintenance.requeue_stalled_deliveries(), 0)

    # --- numberless groups --------------------------------------------------

    def test_a_group_that_never_got_a_number_gets_one_later(self):
        orphan = Conversation.start_group(self.alice, name='Orphan')
        self.assertFalse(GroupNumber.objects.filter(conversation=orphan).exists())

        # A provider that has something to sell. Without one there is nothing
        # to buy and the backfill correctly does nothing, which would test the
        # opposite of what this is about.
        bought = {'number': '+14155559999', 'provider': 'telnyx',
                  'provider_number_id': 'num-1'}
        with mock.patch('sms.provisioning.messaging_is_configured',
                        return_value=True):
            with mock.patch('sms.provisioning.order_number', return_value=bought):
                self.assertEqual(maintenance.provision_missing_numbers(), 1)

        self.assertTrue(GroupNumber.objects.filter(conversation=orphan).exists())

    def test_a_group_that_already_has_one_is_left_alone(self):
        # Rule 2: the number never changes.
        before = GroupNumber.objects.get(conversation=self.group).number
        maintenance.provision_missing_numbers()
        self.assertEqual(
            GroupNumber.objects.get(conversation=self.group).number, before
        )

    # --- retention ----------------------------------------------------------

    def test_old_records_are_purged(self):
        row = self.a_failed_delivery()
        SmsWebhookEvent.objects.create(provider_event_id='old-1')
        old = timezone.now() - timedelta(days=settings.SMS_RETENTION_DAYS + 1)
        SmsDelivery.objects.filter(pk=row.pk).update(created_at=old)
        SmsWebhookEvent.objects.filter(provider_event_id='old-1').update(received_at=old)

        maintenance.purge_old_deliveries()

        self.assertFalse(SmsDelivery.objects.filter(pk=row.pk).exists())
        self.assertFalse(SmsWebhookEvent.objects.filter(provider_event_id='old-1').exists())

    def test_recent_records_survive(self):
        row = self.a_failed_delivery()
        maintenance.purge_old_deliveries()
        self.assertTrue(SmsDelivery.objects.filter(pk=row.pk).exists())

    # --- the report ---------------------------------------------------------

    def test_the_daily_report_counts_what_was_spent(self):
        create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=self.sender,
        )
        body = maintenance.daily_spend_report()
        self.assertIn('billed segments', body)
        self.assertIn('numbers leased', body)

    def test_the_report_says_loudly_when_a_budget_stopped_sending(self):
        # A budget that stops sending does so silently; the first sign
        # otherwise is somebody asking why their group went quiet.
        with override_settings(SMS_MAX_SEGMENTS_PER_DAY=0):
            create_message(
                conversation=self.group, sender=self.alice, text='Dinner at 7',
                participant=self.sender,
            )
        self.assertIn('over budget', maintenance.daily_spend_report())


class GroupSmsSettingsTests(TransactionTestCase):
    """The group's own two SMS switches, one per direction."""

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(username='alice', password='x')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.join_mode = Conversation.JoinMode.OPEN
        self.group.save(update_fields=['join_mode'])
        GroupNumber.objects.create(
            number='+14155551001', conversation=self.group,
            status=GroupNumber.Status.ACTIVE,
        )
        self.mom = self.group.add_phone_participant('+639171111111', display_name='Mom')
        self.mom.sms_consent = Participant.Consent.GRANTED
        self.mom.save(update_fields=['sms_consent'])
        self.sender = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

    def set(self, **fields):
        for k, v in fields.items():
            setattr(self.group, k, v)
        self.group.save(update_fields=list(fields))

    # --- outbound -----------------------------------------------------------

    def test_delivery_off_stops_texts_going_out_and_says_why(self):
        self.set(sms_delivery_enabled=False)
        message = create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=self.sender,
        )
        row = SmsDelivery.objects.get(message=message, participant=self.mom)
        self.assertEqual(row.status, SmsDelivery.Status.SKIPPED)
        self.assertEqual(row.reason, 'sms is off for this group')

    def test_delivery_on_is_the_default(self):
        message = create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=self.sender,
        )
        row = SmsDelivery.objects.get(message=message, participant=self.mom)
        self.assertEqual(row.status, SmsDelivery.Status.SENT)

    # --- inbound ------------------------------------------------------------

    def test_replies_off_refuses_an_inbound_text(self):
        self.set(sms_replies_enabled=False)
        result = inbound.handle_inbound(self.group, '+639171111111', 'I will be there')

        self.assertEqual(result.action, 'refused_replies_off')
        self.assertIn('does not take replies', result.reply)
        self.assertEqual(Message.objects.count(), 0)

    def test_replies_off_still_lets_them_leave_or_stop(self):
        # A member must always be able to get out, whatever the group allows.
        self.set(sms_replies_enabled=False)
        self.assertEqual(
            inbound.handle_inbound(self.group, '+639171111111', 'STOP').action,
            'opted_out',
        )

    def test_the_two_directions_are_independent(self):
        # Announcements out, no replies in.
        self.set(sms_delivery_enabled=True, sms_replies_enabled=False)
        message = create_message(
            conversation=self.group, sender=self.alice, text='Dinner at 7',
            participant=self.sender,
        )
        self.assertEqual(
            SmsDelivery.objects.get(message=message, participant=self.mom).status,
            SmsDelivery.Status.SENT,
        )
        self.assertEqual(
            inbound.handle_inbound(self.group, '+639171111111', 'ok').action,
            'refused_replies_off',
        )
