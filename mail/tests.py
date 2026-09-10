"""Tests for the email channel.

Four things are worth proving here and the rest follows from them:

  1. The reply address resolves to exactly one membership, survives the
     mangling real mail systems do to an address, and resolves to nothing at
     all when it has been tampered with.
  2. The delivery ladder is asked, and the answers it gives are honoured -
     including the one that is easiest to get wrong, which is that archiving a
     conversation does not stop delivery.
  3. A reply can only be posted by the person the address names.
  4. The webhook is idempotent and refuses an unsigned request.
"""
import base64
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from chat import membership
from chat.models import (
    Attachment,
    ChannelConsent,
    Conversation,
    Message,
    Participant,
)
from chat.services import create_message
from mail import inbound, outbound
from mail.models import EmailDelivery, EmailWebhookEvent
from mail.quoting import strip_quoted_reply
from mail.tokens import participant_id_from, reply_address

REPLY_DOMAIN = 'reply.loopdn.com'


def make_user(username, *, verified=True):
    """An account with a verified address, which is what email delivery needs."""
    user = User.objects.create_user(
        username=username, password='x', email=f'{username}@example.com'
    )
    profile = user.profile
    profile.email_verified = verified
    profile.save(update_fields=['email_verified'])
    return user


@contextmanager
def capture_sent():
    """Run the real render and capture what would have gone to the provider.

    Patched at the HTTP boundary rather than at `send_email`, so the templates
    are genuinely rendered and a missing context key fails the test the same
    way it would fail a send.
    """
    sent = []

    def record(url, payload, **kwargs):
        sent.append(payload)
        return {'id': f'resend-{len(sent)}'}

    with mock.patch('utils.mail._is_configured', return_value=True), \
            mock.patch('utils.mail._post', side_effect=record):
        yield sent


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN)
class ReplyAddressTests(TestCase):
    """The address is a name, not a permission - but it must name one person."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )
        self.alice_row = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

    def test_an_address_resolves_back_to_its_membership(self):
        address = reply_address(self.bob_row)

        self.assertEqual(participant_id_from(address), self.bob_row.pk)

    def test_two_members_of_one_group_get_different_addresses(self):
        # An address built from the conversation alone would be a public write
        # handle to a private group.
        self.assertNotEqual(
            reply_address(self.bob_row), reply_address(self.alice_row)
        )

    def test_case_folding_does_not_break_it(self):
        # The reason the tag is hex rather than base64. Providers and
        # forwarders normalise local parts, and a single folded character in a
        # base64 signature turns a valid address into a forged one.
        address = reply_address(self.bob_row)

        self.assertEqual(participant_id_from(address.upper()), self.bob_row.pk)

    def test_a_tampered_token_resolves_to_nothing(self):
        address = reply_address(self.bob_row)
        local, _, domain = address.partition('@')
        forged = f'{local[:-1]}{"0" if local[-1] != "0" else "1"}@{domain}'

        self.assertIsNone(participant_id_from(forged))

    def test_swapping_the_id_resolves_to_nothing(self):
        # The tag covers the id, so pointing an otherwise valid address at
        # somebody else's membership does not verify.
        _, _, domain = reply_address(self.bob_row).partition('@')
        tag = reply_address(self.bob_row).split('@')[0].split('.')[1]
        forged = f'reply+{self.alice_row.pk}.{tag}@{domain}'

        self.assertIsNone(participant_id_from(forged))

    def test_another_domain_is_not_ours(self):
        address = reply_address(self.bob_row)
        _, _, local = address.partition('reply+')

        self.assertIsNone(participant_id_from(f'reply+{local}'.replace(
            REPLY_DOMAIN, 'example.com'
        )))

    def test_an_ordinary_address_resolves_to_nothing(self):
        for address in ['', 'alice@example.com', 'reply@reply.loopdn.com',
                        'reply+nonsense@reply.loopdn.com']:
            with self.subTest(address=address):
                self.assertIsNone(participant_id_from(address))

    def test_an_address_inside_angle_brackets_resolves(self):
        # A To header is not a bare address. Without parseaddr the trailing
        # ">" stayed on the domain, so every reply from a client that writes a
        # display name resolved to nothing and was dropped in silence.
        address = reply_address(self.bob_row)

        for form in [
            f'<{address}>',
            f'"Family via Loopd" <{address}>',
            f'Family via Loopd <{address}>',
            f'  {address}  ',
        ]:
            with self.subTest(form=form):
                self.assertEqual(participant_id_from(form), self.bob_row.pk)

    @override_settings(MAIL_REPLY_DOMAIN='')
    def test_with_no_reply_domain_there_is_no_address(self):
        self.assertIsNone(reply_address(self.bob_row))


class QuotedReplyTests(TestCase):
    """Finding the part of a reply the sender actually wrote."""

    def test_gmail_style_quoting_is_cut(self):
        text = (
            "I'll be there.\n"
            '\n'
            'On Mon, 8 Sep 2026 at 19:04, Family via Loopd <x@y.com> wrote:\n'
            '> Dinner at 7 PM.\n'
        )

        self.assertEqual(strip_quoted_reply(text), "I'll be there.")

    def test_an_outlook_header_block_is_cut(self):
        text = (
            'On my way.\n'
            '\n'
            'From: Family via Loopd\n'
            'Sent: Monday, 8 September 2026\n'
            'Subject: New message in Family\n'
        )

        self.assertEqual(strip_quoted_reply(text), 'On my way.')

    def test_a_signature_is_cut(self):
        text = 'Sounds good.\n\n-- \nSent from my phone\n'

        self.assertEqual(strip_quoted_reply(text), 'Sounds good.')

    def test_trailing_quoted_lines_are_dropped(self):
        text = 'Yes\n\n> Dinner at 7 PM.\n> - Chester\n'

        self.assertEqual(strip_quoted_reply(text), 'Yes')

    def test_inline_replies_are_kept(self):
        # A quote in the MIDDLE is somebody answering point by point, and
        # dropping it would throw away half of what they wrote.
        text = '> Are you coming?\nYes\n\n> Bringing anything?\nDessert\n'

        self.assertEqual(
            strip_quoted_reply(text),
            '> Are you coming?\nYes\n\n> Bringing anything?\nDessert',
        )

    def test_a_line_that_merely_starts_with_from_is_not_a_quote(self):
        # `From:` alone is prose. It only marks a quote when a second header
        # line sits directly under it, or a whole message gets cut in half.
        text = 'Got the quote back\n\nFrom: the supplier, it is 400\n'

        self.assertEqual(
            strip_quoted_reply(text), 'Got the quote back\n\nFrom: the supplier, it is 400'
        )

    def test_something_it_does_not_recognise_is_left_alone(self):
        # The safe failure: untidy beats truncated.
        text = 'Dinner works for me, see you then'

        self.assertEqual(strip_quoted_reply(text), text)

    def test_a_reply_that_is_nothing_but_a_quote_comes_back_empty(self):
        self.assertEqual(strip_quoted_reply('> Dinner at 7 PM.\n'), '')

    def test_a_bottom_posted_reply_is_kept(self):
        # Outlook and most of the world outside Gmail write UNDER the thread.
        # Cutting at the first separator returned an empty string, and an empty
        # reply is discarded - so every one of those messages was lost silently.
        text = (
            'On Mon, 8 Sep 2026 at 19:04, Family via Loopd <x@y.com> wrote:\n'
            '> Dinner at 7 PM.\n'
            '\n'
            "Yes, I'll bring the wine and be there by 6.30.\n"
        )

        self.assertEqual(
            strip_quoted_reply(text),
            "Yes, I'll bring the wine and be there by 6.30.",
        )

    def test_a_bottom_posted_reply_under_an_outlook_block_is_kept(self):
        text = (
            'From: Family via Loopd\n'
            'Sent: Monday, 8 September 2026\n'
            'Subject: New message in Family\n'
            '\n'
            'On my way.\n'
        )

        self.assertEqual(strip_quoted_reply(text), 'On my way.')

    def test_top_posting_still_wins_when_there_is_text_both_sides(self):
        # The ordinary case must not change: what they wrote is above.
        text = (
            'See you there.\n'
            '\n'
            'On Mon, Family via Loopd wrote:\n'
            '> Dinner at 7 PM.\n'
            '\n'
            'Sent from my phone\n'
        )

        self.assertEqual(strip_quoted_reply(text), 'See you there.')


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN)
class EmailFanOutTests(TestCase):
    """Who gets a group's messages by email, and who does not."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )

    def post(self, text='Dinner at 7 PM.'):
        return create_message(self.group, self.alice, text=text)

    def audience(self):
        send_to, skipped = outbound.recipients_for(self.post())
        return [row.pk for row in send_to], dict(
            (row.pk, reason) for row, reason in skipped
        )

    # --- who receives -------------------------------------------------------

    def test_a_member_with_a_verified_address_receives(self):
        send_to, _ = self.audience()

        self.assertEqual(send_to, [self.bob_row.pk])

    def test_the_sender_never_receives_their_own_message(self):
        send_to, skipped = self.audience()
        alice_row = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

        self.assertNotIn(alice_row.pk, send_to)
        self.assertNotIn(alice_row.pk, skipped)

    def test_archiving_does_not_stop_email(self):
        # The rule most easily got wrong. Archive hides a conversation from a
        # list; muting is how somebody asks for silence.
        membership.set_archived(self.bob_row, True)

        send_to, _ = self.audience()

        self.assertEqual(send_to, [self.bob_row.pk])

    # --- who does not, and why ----------------------------------------------

    def test_a_muted_member_does_not(self):
        membership.set_muted(self.bob_row, True)

        send_to, skipped = self.audience()

        self.assertEqual(send_to, [])
        self.assertEqual(skipped[self.bob_row.pk], 'muted')

    def test_a_member_who_declined_email_does_not(self):
        membership.set_channel_consent(
            self.bob_row,
            ChannelConsent.Channel.EMAIL,
            ChannelConsent.Status.DECLINED,
        )

        send_to, skipped = self.audience()

        self.assertEqual(send_to, [])
        self.assertEqual(skipped[self.bob_row.pk], 'declined this group')

    def test_declining_email_leaves_sms_alone(self):
        # The channels are separate answers to separate questions.
        membership.set_channel_consent(
            self.bob_row,
            ChannelConsent.Channel.EMAIL,
            ChannelConsent.Status.DECLINED,
        )

        self.assertEqual(
            membership.consent_status(self.bob_row, ChannelConsent.Channel.SMS),
            ChannelConsent.Status.OPTED_IN,
        )

    def test_email_switched_off_account_wide_does_not(self):
        profile = self.bob.profile
        profile.email_enabled = False
        profile.save(update_fields=['email_enabled'])

        send_to, skipped = self.audience()

        self.assertEqual(send_to, [])
        self.assertEqual(skipped[self.bob_row.pk], 'email disabled account-wide')

    def test_an_unverified_address_does_not(self):
        # The whole gate, and the same one a number has. An unverified address
        # is a string somebody typed, and mailing it publishes a private group
        # to whoever really owns it.
        profile = self.bob.profile
        profile.email_verified = False
        profile.save(update_fields=['email_verified'])

        send_to, skipped = self.audience()

        self.assertEqual(send_to, [])
        self.assertEqual(skipped[self.bob_row.pk], 'no verified address')

    def test_a_phone_only_member_does_not(self):
        # Refused at consent rather than at the address, because a row with no
        # account has answered nothing - and the ladder asks that first.
        row = self.group.add_phone_participant('+639171111111', 'Mom')

        _, skipped = self.audience()

        self.assertEqual(skipped[row.pk], 'consent not granted')

    def test_and_would_be_refused_for_want_of_an_address_anyway(self):
        row = self.group.add_phone_participant('+639171111111', 'Mom')

        self.assertEqual(outbound._address_reason(row), 'no verified address')

    def test_a_member_who_left_does_not(self):
        self.bob_row.status = Participant.Status.LEFT
        self.bob_row.save(update_fields=['status'])

        send_to, skipped = self.audience()

        self.assertEqual(send_to, [])
        self.assertEqual(skipped, {})

    # --- the send itself ----------------------------------------------------

    def test_the_message_is_sent_and_logged(self):
        with capture_sent() as sent:
            rows = outbound.fan_out(self.post())

        self.assertEqual(len(sent), 1)
        self.assertEqual([row.status for row in rows], [EmailDelivery.Status.SENT])
        self.assertEqual(rows[0].counterpart_domain, 'example.com')

    def test_the_address_is_never_stored_in_full(self):
        with capture_sent():
            rows = outbound.fan_out(self.post())

        self.assertNotIn('bob@example.com', str(rows[0].__dict__))
        self.assertEqual(len(rows[0].counterpart_hash), 64)

    def test_it_comes_from_the_group(self):
        with capture_sent() as sent:
            outbound.fan_out(self.post())

        self.assertTrue(sent[0]['from'].startswith('Family via '))

    def test_a_group_name_with_a_comma_is_quoted(self):
        # Otherwise `Smith, Jane via Loopd <x@y>` parses as two recipients.
        self.group.name = 'Smith, Jane'
        self.group.save(update_fields=['name'])

        with capture_sent() as sent:
            outbound.fan_out(self.post())

        self.assertTrue(sent[0]['from'].startswith('"Smith, Jane via '))

    def test_a_newline_in_a_group_name_does_not_break_every_send(self):
        # A group name is user input and nothing upstream forbids a newline.
        # utils/mail rightly refuses such a From, so without collapsing it here
        # one group's name silently broke all of its email, permanently.
        self.group.name = 'Fam\nily'
        self.group.save(update_fields=['name'])

        with capture_sent() as sent:
            rows = outbound.fan_out(self.post())

        self.assertEqual(len(sent), 1)
        self.assertEqual(rows[0].status, EmailDelivery.Status.SENT)
        self.assertNotIn('\n', sent[0]['from'])

    def test_it_identifies_itself_as_automatic(self):
        # RFC 3834. Without it a vacation responder answers, and that reply is
        # posted into the group and fanned back out to everybody.
        with capture_sent() as sent:
            outbound.fan_out(self.post())

        self.assertEqual(
            sent[0]['headers']['Auto-Submitted'], 'auto-generated'
        )

    def test_the_reply_address_is_the_recipients_own(self):
        with capture_sent() as sent:
            outbound.fan_out(self.post())

        self.assertEqual(
            participant_id_from(sent[0]['reply_to']), self.bob_row.pk
        )

    def test_the_body_carries_the_message_and_the_sender(self):
        with capture_sent() as sent:
            outbound.fan_out(self.post())

        self.assertIn('Dinner at 7 PM.', sent[0]['text'])
        self.assertIn('alice', sent[0]['text'])

    def test_every_omission_is_recorded_with_its_reason(self):
        membership.set_muted(self.bob_row, True)

        with capture_sent():
            rows = outbound.fan_out(self.post())

        self.assertEqual([row.reason for row in rows], ['muted'])
        self.assertEqual(rows[0].status, EmailDelivery.Status.SKIPPED)

    def test_a_second_fan_out_does_not_send_again(self):
        message = self.post()

        with capture_sent() as sent:
            outbound.fan_out(message)
            outbound.fan_out(message)

        self.assertEqual(len(sent), 1)
        self.assertEqual(
            EmailDelivery.objects.filter(message=message).count(), 1
        )

    def test_a_failed_send_does_not_stop_the_others(self):
        carol = make_user('carol')
        self.group.add_participant(carol)
        from utils.mail import EmailError

        calls = []

        def one_bad_address(*args, **kwargs):
            calls.append(kwargs.get('to'))
            if len(calls) == 1:
                raise EmailError('nope')
            return 'resend-2'

        with mock.patch('mail.outbound.send_templated_email',
                        side_effect=one_bad_address):
            rows = outbound.fan_out(self.post())

        self.assertEqual(sorted(row.status for row in rows), ['failed', 'sent'])

    @override_settings(MAIL_MAX_RECIPIENTS_PER_MESSAGE=1)
    def test_a_group_over_the_cap_sends_to_nobody(self):
        self.group.add_participant(make_user('carol'))

        with capture_sent() as sent:
            rows = outbound.fan_out(self.post())

        self.assertEqual(sent, [])
        self.assertEqual(
            {row.reason for row in rows}, {'group is too large to email'}
        )

    def test_a_message_with_only_an_attachment_still_says_something(self):
        # Otherwise the recipient gets "alice:" and nothing else, which reads
        # as a bug in the product rather than as a photo they cannot see here.
        message = self.post(text='')
        Attachment.objects.create(
            message=message, file='x.jpg', kind=Attachment.Kind.IMAGE
        )

        self.assertIn('open Loopd', outbound.body_for(message))

    def test_and_several_of_them_say_how_many(self):
        message = self.post(text='')
        for name in ('a.jpg', 'b.jpg'):
            Attachment.objects.create(
                message=message, file=name, kind=Attachment.Kind.IMAGE
            )

        self.assertIn('2 photos', outbound.body_for(message))


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN)
class EmailReplyTests(TestCase):
    """A reply posts to the group - if it really came from the member."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )
        self.address = reply_address(self.bob_row)

    def reply(self, body="I'll be there.", sender='bob@example.com', to=None):
        return inbound.handle_inbound(to or self.address, sender, body)

    def test_a_reply_becomes_a_message_in_the_group(self):
        result = self.reply()

        self.assertEqual(result.action, 'posted')
        self.assertEqual(self.group.messages.count(), 1)
        self.assertEqual(self.group.messages.first().text, "I'll be there.")

    def test_it_is_recorded_as_having_come_from_email(self):
        result = self.reply()

        self.assertEqual(result.message.source, Message.Source.EMAIL)

    def test_it_is_credited_to_the_member_the_address_names(self):
        result = self.reply()

        self.assertEqual(result.message.sender_participant_id, self.bob_row.pk)

    def test_the_quoted_thread_does_not_come_with_it(self):
        result = self.reply(
            "I'll be there.\n\nOn Mon, Family via Loopd wrote:\n> Dinner at 7\n"
        )

        self.assertEqual(result.message.text, "I'll be there.")

    def test_a_display_name_around_the_address_is_fine(self):
        result = self.reply(sender='Bob <Bob@Example.com>')

        self.assertEqual(result.action, 'posted')

    # --- what is refused, silently ------------------------------------------

    def test_a_reply_from_another_address_is_dropped(self):
        # The check doing the real work: whoever holds the address can send
        # mail TO it, and only the mailbox owner can send mail FROM it.
        result = self.reply(sender='mallory@example.com')

        self.assertEqual(result.action, 'ignored_address_mismatch')
        self.assertEqual(self.group.messages.count(), 0)

    def test_a_reply_from_somebody_who_left_is_dropped(self):
        # The address outlives the membership - it is in the headers of every
        # message they were ever sent.
        membership.leave(self.bob_row)

        result = self.reply()

        self.assertEqual(result.action, 'ignored_not_a_member')
        self.assertEqual(self.group.messages.count(), 0)

    def test_a_reply_from_a_blocked_member_is_dropped(self):
        membership.remove(self.bob_row, self.alice, block=True)

        result = self.reply()

        self.assertEqual(result.action, 'ignored_not_a_member')

    def test_a_reply_to_an_unknown_address_is_dropped(self):
        result = self.reply(to='reply+9999.0000000000000000abcd@reply.loopdn.com')

        self.assertEqual(result.action, 'ignored_unknown_recipient')

    def test_a_reply_from_an_unverified_account_is_dropped(self):
        profile = self.bob.profile
        profile.email_verified = False
        profile.save(update_fields=['email_verified'])

        result = self.reply()

        self.assertEqual(result.action, 'ignored_address_mismatch')

    def test_an_auto_reply_with_nothing_in_it_is_dropped(self):
        result = self.reply('> Dinner at 7 PM.\n')

        self.assertEqual(result.action, 'ignored_empty')
        self.assertEqual(self.group.messages.count(), 0)

    def test_a_broadcast_group_refuses_replies(self):
        self.group.mode = Conversation.Mode.BROADCAST
        self.group.save(update_fields=['mode'])

        result = self.reply()

        self.assertEqual(result.action, 'refused_broadcast')
        self.assertEqual(self.group.messages.count(), 0)

    # --- what is allowed, and is easy to get wrong --------------------------

    def test_a_muted_member_can_still_reply(self):
        # Mute means "do not notify me". It does not mean "do not let me take
        # part", and conflating the two silently removes somebody's voice.
        membership.set_muted(self.bob_row, True)

        self.assertEqual(self.reply().action, 'posted')

    def test_an_archived_member_can_still_reply(self):
        membership.set_archived(self.bob_row, True)

        self.assertEqual(self.reply().action, 'posted')

    def test_a_member_who_declined_email_can_still_reply(self):
        # Consent is about receiving. Somebody replying to an old email is
        # still a member with something to say.
        membership.set_channel_consent(
            self.bob_row,
            ChannelConsent.Channel.EMAIL,
            ChannelConsent.Status.DECLINED,
        )

        self.assertEqual(self.reply().action, 'posted')


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, RESEND_WEBHOOK_SECRET='')
class InboundWebhookTests(APITestCase):
    """The public endpoint. Signature, de-duplication, and the status codes."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )
        self.url = reverse('mail:inbound-webhook')

    def payload(self, body="I'll be there.", event_id='evt-1'):
        return {
            'type': 'email.received',
            'data': {
                'id': event_id,
                'to': [reply_address(self.bob_row)],
                'from': 'bob@example.com',
                'text': body,
            },
        }

    def post(self, payload, **headers):
        return self.client.post(
            self.url, data=json.dumps(payload),
            content_type='application/json', **headers,
        )

    def test_a_reply_arrives_and_is_posted(self):
        response = self.post(self.payload())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group.messages.count(), 1)

    def test_it_is_logged_as_inbound(self):
        self.post(self.payload())

        row = EmailDelivery.objects.get(direction=EmailDelivery.Direction.INBOUND)
        self.assertEqual(row.participant_id, self.bob_row.pk)
        self.assertEqual(row.counterpart_domain, 'example.com')

    def test_the_same_event_twice_posts_once(self):
        # A provider retries anything it does not read as success. Without the
        # de-duplication row that is the same reply in the group twice.
        self.post(self.payload())
        response = self.post(self.payload())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group.messages.count(), 1)

    def test_a_different_event_posts_again(self):
        self.post(self.payload(event_id='evt-1'))
        self.post(self.payload(body='So am I.', event_id='evt-2'))

        self.assertEqual(self.group.messages.count(), 2)

    def test_a_delivery_receipt_is_accepted_and_ignored(self):
        response = self.post({'type': 'email.delivered', 'data': {'id': 'evt-9'}})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group.messages.count(), 0)

    def test_an_unparseable_body_is_not_retried(self):
        response = self.client.post(
            self.url, data='not json', content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_failure_asks_for_a_retry_and_frees_the_event(self):
        # Leaving the claim behind would turn the retry into a no-op and the
        # reply would be lost silently, forever.
        with mock.patch('mail.inbound.handle_inbound',
                        side_effect=RuntimeError('boom')):
            response = self.post(self.payload())

        self.assertEqual(response.status_code, 500)
        self.assertFalse(
            EmailWebhookEvent.objects.filter(provider_event_id='evt-1').exists()
        )

    def test_an_html_only_reply_still_posts(self):
        payload = self.payload()
        payload['data'].pop('text')
        payload['data']['html'] = '<p>On my way</p>'

        self.post(payload)

        self.assertEqual(self.group.messages.first().text, 'On my way')


@override_settings(
    MAIL_REPLY_DOMAIN=REPLY_DOMAIN,
    RESEND_WEBHOOK_SECRET='whsec_' + base64.b64encode(b'secret-key').decode(),
)
class WebhookSignatureTests(APITestCase):
    """With a secret configured, an unsigned request must not get in.

    This endpoint posts into private groups, so its signature is the only thing
    standing between the internet and somebody else's family conversation.
    """

    def setUp(self):
        self.url = reverse('mail:inbound-webhook')
        self.body = json.dumps({'type': 'email.delivered', 'data': {'id': 'e1'}})

    def signed_headers(self, body, *, event_id='msg_1', timestamp=None, key=b'secret-key'):
        timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
        signed = f'{event_id}.{timestamp}.'.encode() + body.encode()
        digest = base64.b64encode(
            hmac.new(key, signed, hashlib.sha256).digest()
        ).decode()
        return {
            'HTTP_SVIX_ID': event_id,
            'HTTP_SVIX_TIMESTAMP': timestamp,
            'HTTP_SVIX_SIGNATURE': f'v1,{digest}',
        }

    def post(self, **headers):
        return self.client.post(
            self.url, data=self.body, content_type='application/json', **headers
        )

    def test_a_correctly_signed_request_is_accepted(self):
        response = self.post(**self.signed_headers(self.body))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_an_unsigned_request_is_refused(self):
        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_signature_from_the_wrong_key_is_refused(self):
        response = self.post(**self.signed_headers(self.body, key=b'other-key'))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_replayed_request_is_refused_once_it_is_stale(self):
        # The timestamp is inside the signed material for exactly this: without
        # it a captured request could be replayed verbatim forever.
        old = int(time.time()) - 4000
        response = self.post(**self.signed_headers(self.body, timestamp=old))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_body_that_was_altered_after_signing_is_refused(self):
        headers = self.signed_headers(self.body)
        self.body = json.dumps({'type': 'email.received', 'data': {'id': 'e2'}})

        self.assertEqual(self.post(**headers).status_code, 403)


@override_settings(
    MAIL_SENDING_ENABLED=True, MAIL_REPLY_DOMAIN=REPLY_DOMAIN
)
class WiredUpTests(TransactionTestCase):
    """Posting a message really does send the email.

    TransactionTestCase, because the fan-out is queued with
    `transaction.on_commit` - deliberately, so nothing is sent about a message
    that then rolls back. A plain TestCase never commits, so all of this would
    look unwired.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)

    def test_a_new_message_is_emailed_to_the_group(self):
        with capture_sent() as sent:
            create_message(self.group, self.alice, text='Dinner at 7 PM.')

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['to'], ['bob@example.com'])

    @override_settings(MAIL_SENDING_ENABLED=False)
    def test_with_the_channel_off_nothing_is_sent(self):
        with capture_sent() as sent:
            create_message(self.group, self.alice, text='Dinner at 7 PM.')

        self.assertEqual(sent, [])

    def test_one_channel_failing_does_not_fail_the_message(self):
        # schedule_all catches per channel. A message that is already stored
        # and already broadcast must not 500 because email is misconfigured.
        with mock.patch('mail.outbound.schedule', side_effect=RuntimeError('boom')):
            message = create_message(self.group, self.alice, text='Dinner')

        self.assertIsNotNone(message.pk)


class ChannelConsentDefaultTests(TestCase):
    """Every membership has an answer on record for email, from the start."""

    def setUp(self):
        self.alice = make_user('alice')
        self.group = Conversation.start_group(self.alice, name='Family')

    def test_an_account_member_is_opted_in(self):
        bob = make_user('bob')
        self.group.add_participant(bob)
        row = Participant.objects.get(conversation=self.group, user=bob)

        self.assertEqual(
            membership.consent_status(row, ChannelConsent.Channel.EMAIL),
            ChannelConsent.Status.OPTED_IN,
        )

    def test_a_phone_only_member_is_pending(self):
        row = self.group.add_phone_participant('+639171111111', 'Mom')

        self.assertEqual(
            membership.consent_status(row, ChannelConsent.Channel.EMAIL),
            ChannelConsent.Status.PENDING,
        )

    def test_claiming_a_phone_row_grants_email_and_not_sms(self):
        # Verifying an address proves who is reading it. Nobody confirmed the
        # phone number by text, so its answer is still open.
        row = self.group.add_phone_participant('+639171111111', 'Mom')
        mom = make_user('mom')
        profile = mom.profile
        profile.phone_number = '+639171111111'
        profile.phone_no_verified = True
        profile.save(update_fields=['phone_number', 'phone_no_verified'])

        self.group.add_participant(mom)
        row.refresh_from_db()

        self.assertEqual(
            membership.consent_status(row, ChannelConsent.Channel.EMAIL),
            ChannelConsent.Status.OPTED_IN,
        )
        self.assertEqual(
            membership.consent_status(row, ChannelConsent.Channel.SMS),
            ChannelConsent.Status.PENDING,
        )
