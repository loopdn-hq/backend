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

from django.conf import settings
from django.contrib.auth.models import User
from datetime import timedelta

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from chat import delivery, membership
from chat.models import (
    Attachment,
    ChannelConsent,
    Conversation,
    Message,
    Participant,
)
from chat.services import create_message
from mail import (
    authentication,
    autoreply,
    commands,
    inbound,
    maintenance,
    outbound,
    provider,
    receipts,
)
from mail.models import EmailDelivery, EmailWebhookEvent
from mail.quoting import strip_quoted_reply
from mail.tokens import (
    participant_id_from,
    participant_id_from_unsubscribe,
    reply_address,
    unsubscribe_token_for,
    unsubscribe_url,
)
from utils.mail import EmailError
from user.models import MessagingSettings

REPLY_DOMAIN = 'reply.loopdn.com'
API_URL = 'https://api.loopdn.com'


def make_user(username, *, verified=True):
    """An account with a verified address, which is what email delivery needs."""
    user = User.objects.create_user(
        username=username, password='x', email=f'{username}@example.com'
    )
    profile = user.profile
    profile.email_verified = verified
    profile.save(update_fields=['email_verified'])
    return user


def authenticated(domain='example.com'):
    """Headers as the receiving server writes them for a genuine reply from `domain`."""
    return {
        'authentication-results': (
            f'mx.resend.com; dkim=pass header.d={domain}; '
            f'spf=pass smtp.mailfrom=bounce.{domain}; '
            f'dmarc=pass header.from={domain}'
        ),
    }


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
        switches = self.bob.messaging_settings
        switches.email_enabled = False
        switches.save(update_fields=['email_enabled'])

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

    def reply(self, body="I'll be there.", sender='bob@example.com', to=None,
              headers=None):
        return inbound.handle_inbound(
            to or self.address, sender, body,
            headers=authenticated() if headers is None else headers,
        )

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

    def test_a_reply_the_server_did_not_authenticate_is_dropped(self):
        # The From address is only worth comparing once somebody has vouched
        # for it. Without that, it is a string anybody can type.
        result = self.reply(headers={
            'authentication-results': (
                'mx.resend.com; dkim=fail header.d=example.com; '
                'spf=fail smtp.mailfrom=example.com; dmarc=fail header.from=example.com'
            ),
        })

        self.assertEqual(result.action, 'ignored_unauthenticated')
        self.assertEqual(self.group.messages.count(), 0)

    def test_a_reply_with_no_verdict_at_all_is_dropped(self):
        # Fail closed. Absent must not mean trusted, or stripping one header
        # is the way in - and whether the provider writes it at all is exactly
        # what a live account has to confirm before replies go on.
        result = self.reply(headers={})

        self.assertEqual(result.action, 'ignored_unauthenticated')

    def test_a_passing_signature_for_somebody_elses_domain_does_not_count(self):
        # mallory.net validly signs a message whose From claims example.com.
        result = self.reply(headers=authenticated('mallory.net'))

        self.assertEqual(result.action, 'ignored_unauthenticated')

    def test_authentication_is_asked_before_the_address_is_compared(self):
        # A forgery is logged as a forgery, not as a mismatch.
        result = self.reply(sender='mallory@example.com', headers={})

        self.assertEqual(result.action, 'ignored_unauthenticated')

    @override_settings(MAIL_INBOUND_REQUIRE_AUTHENTICATION=False)
    def test_with_the_requirement_off_the_address_alone_is_trusted(self):
        # A laptop feeding the webhook by hand. mail.E003 refuses to deploy it.
        result = self.reply(headers={})

        self.assertEqual(result.action, 'posted')

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

    # --- what is refused, because they turned email off --------------------

    def test_a_member_who_stopped_this_groups_email_cannot_reply_to_it(self):
        # STOP EMAILS, or the footer link, stops this group in both directions:
        # it no longer emails them, and no longer takes their replies.
        membership.set_channel_consent(
            self.bob_row,
            ChannelConsent.Channel.EMAIL,
            ChannelConsent.Status.DECLINED,
        )

        result = self.reply()

        self.assertEqual(result.action, 'ignored_email_declined')
        self.assertEqual(self.group.messages.count(), 0)

    def test_a_member_with_email_off_cannot_reply_to_any_group(self):
        # "Send and receive Loop messages by email", unticked in the app.
        MessagingSettings.objects.filter(user=self.bob).update(email_enabled=False)

        result = self.reply()

        self.assertEqual(result.action, 'ignored_email_off')
        self.assertEqual(self.group.messages.count(), 0)


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

        # The event carries no body. What the fetch returns is set up here and
        # varied per test; which ids were fetched is recorded.
        self.content = {'text': "I'll be there.", 'html': '', 'headers': authenticated()}
        self.fetched = []
        patcher = mock.patch('mail.provider.fetch_received', side_effect=self.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, email_id):
        self.fetched.append(email_id)
        return provider.ReceivedEmail(
            email_id=email_id,
            from_address='bob@example.com',
            to_address=reply_address(self.bob_row),
            text=self.content['text'],
            html=self.content['html'],
            headers=self.content['headers'],
        )

    def payload(self, body=None, event_id='evt-1', to=None):
        """One `email.received` event, as Resend posts it: metadata only."""
        if body is not None:
            self.content['text'] = body
        return {
            'type': 'email.received',
            'data': {
                'email_id': event_id,
                'to': to or [reply_address(self.bob_row)],
                'from': 'bob@example.com',
                'subject': 'Re: Family',
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
        # The event was a notice; the words came from the second call.
        self.assertEqual(self.fetched, ['evt-1'])

    def test_nothing_is_fetched_for_an_address_we_did_not_issue(self):
        # Junk sent at the reply domain must not cost an API call each.
        response = self.post(self.payload(to=[f'someone@{REPLY_DOMAIN}']))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.group.messages.count(), 0)

    def test_a_reply_all_that_puts_us_second_still_finds_us(self):
        response = self.post(self.payload(
            to=['friend@example.com', reply_address(self.bob_row)]
        ))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group.messages.count(), 1)

    def test_a_fetch_failure_asks_for_a_retry_and_frees_the_event(self):
        with mock.patch('mail.provider.fetch_received',
                        side_effect=EmailError('provider down')):
            response = self.post(self.payload())

        self.assertEqual(response.status_code, 500)
        self.assertFalse(
            EmailWebhookEvent.objects.filter(provider_event_id='evt-1').exists()
        )

    def test_an_unauthenticated_reply_is_accepted_and_dropped(self):
        # 200, because the provider will not change its mind about the
        # headers on retry; and nothing posted, because nobody vouched for
        # the From address.
        self.content['headers'] = {}

        response = self.post(self.payload())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group.messages.count(), 0)

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
        self.content.update(text='', html='<p>On my way</p>')

        self.post(self.payload())

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


@override_settings(
    MAIL_SENDING_ENABLED=True, MAIL_REPLY_DOMAIN=REPLY_DOMAIN
)
class DirectMessagesAreNotEmailedTests(TransactionTestCase):
    """A direct message never leaves the app, on any channel.

    Found live: a direct message fanned out through the GROUP template, signed
    " via Loopd" with the group name blank and ending in a footer for leaving
    a group that did not exist. Only `MAIL_SENDING_ENABLED=False` in
    production kept it in. The rule is `chat/delivery.py:conversation_reason`.

    TransactionTestCase for the reason WiredUpTests is: the first test here is
    the live reproduction, through create_message and on_commit.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.dm, _ = Conversation.start_direct(self.alice, self.bob)

    def test_a_direct_message_is_not_emailed(self):
        # Both have verified addresses and are opted in - in a group the same
        # two people ARE emailed, which WiredUpTests holds.
        with capture_sent() as sent:
            create_message(self.dm, self.alice, text='Are you free later?')

        self.assertEqual(sent, [])
        self.assertFalse(EmailDelivery.objects.exists())

    def test_the_audience_is_empty_on_every_channel(self):
        message = create_message(self.dm, self.alice, text='hi')

        for channel in ChannelConsent.Channel:
            with self.subTest(channel=channel):
                self.assertEqual(
                    delivery.eligible_for(message, channel), ([], [])
                )

    def test_a_fan_out_reached_by_hand_still_finds_nobody(self):
        # Not only the scheduler. The Celery task re-run from a shell, or a
        # "resend" button written later, goes straight to fan_out.
        message = create_message(self.dm, self.alice, text='hi')

        with capture_sent() as sent:
            rows = outbound.fan_out(message)

        self.assertEqual(rows, [])
        self.assertEqual(sent, [])

    def test_no_channel_is_even_offered_a_direct_message(self):
        # Through the write path itself: create_message hands every message to
        # schedule_all, which is where a direct one must stop.
        offered = []
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)

        with mock.patch.dict(
            'chat.delivery._channels', {'probe': offered.append}, clear=True
        ):
            create_message(self.dm, self.alice, text='dm')
            create_message(group, self.alice, text='group')

        self.assertEqual([m.text for m in offered], ['group'])

    def test_the_reason_names_the_kind(self):
        group = Conversation.start_group(self.alice, name='Family')

        self.assertEqual(
            delivery.conversation_reason(self.dm),
            'direct conversations are not delivered externally',
        )
        self.assertIsNone(delivery.conversation_reason(group))


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


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, API_PUBLIC_URL=API_URL)
class UnsubscribeTokenTests(TestCase):
    """The link is a name, not a permission - and not the reply address."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )

    def test_a_token_resolves_back_to_its_membership(self):
        token = unsubscribe_token_for(self.bob_row.pk)

        self.assertEqual(participant_id_from_unsubscribe(token), self.bob_row.pk)

    def test_a_reply_token_is_not_an_unsubscribe_token(self):
        # The reason the two salts differ, and the security boundary of the
        # whole feature: the reply address is printed in every group email and
        # travels in every forward of it. If the tags were interchangeable,
        # holding one would be holding both.
        reply_tag = reply_address(self.bob_row).split('@')[0].split('.')[1]

        self.assertIsNone(
            participant_id_from_unsubscribe(f'{self.bob_row.pk}.{reply_tag}')
        )

    def test_and_an_unsubscribe_token_is_not_a_reply_address(self):
        tag = unsubscribe_token_for(self.bob_row.pk).split('.')[1]

        self.assertIsNone(
            participant_id_from(f'reply+{self.bob_row.pk}.{tag}@{REPLY_DOMAIN}')
        )

    def test_a_tampered_tag_resolves_to_nothing(self):
        token = unsubscribe_token_for(self.bob_row.pk)
        forged = f'{token[:-1]}{"0" if token[-1] != "0" else "1"}'

        self.assertIsNone(participant_id_from_unsubscribe(forged))

    def test_pointing_a_valid_tag_at_somebody_else_resolves_to_nothing(self):
        tag = unsubscribe_token_for(self.bob_row.pk).split('.')[1]
        alice_row = Participant.objects.get(
            conversation=self.group, user=self.alice
        )

        self.assertIsNone(
            participant_id_from_unsubscribe(f'{alice_row.pk}.{tag}')
        )

    def test_nonsense_resolves_to_nothing(self):
        for token in ['', 'nonsense', '1.', '.abc', '1.NOTHEX0000000000000']:
            with self.subTest(token=token):
                self.assertIsNone(participant_id_from_unsubscribe(token))

    def test_the_url_is_short_and_url_safe(self):
        url = unsubscribe_url(self.bob_row)

        self.assertTrue(url.startswith(f'{API_URL}/mail/unsubscribe/'))
        self.assertTrue(url.endswith('/'))
        self.assertNotIn('api/v1', url)
        # Wrapped URLs break in text/plain bodies.
        self.assertLess(len(url), 78)

    @override_settings(API_PUBLIC_URL='')
    def test_with_no_public_url_there_is_no_link(self):
        self.assertIsNone(unsubscribe_url(self.bob_row))


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, API_PUBLIC_URL=API_URL)
class UnsubscribeEndpointTests(APITestCase):
    """The public page. Idempotent, inert on GET, and never an oracle."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.family = Conversation.start_group(self.alice, name='Family')
        self.work = Conversation.start_group(self.alice, name='Work')
        self.family.add_participant(self.bob)
        self.work.add_participant(self.bob)
        self.bob_family = Participant.objects.get(
            conversation=self.family, user=self.bob
        )
        self.bob_work = Participant.objects.get(
            conversation=self.work, user=self.bob
        )

    def url(self, participant=None):
        token = unsubscribe_token_for((participant or self.bob_family).pk)
        return reverse('mail-unsubscribe', args=[token])

    def email_consent(self, participant):
        return membership.consent_status(
            participant, ChannelConsent.Channel.EMAIL
        )

    # --- GET writes nothing -------------------------------------------------

    def test_get_changes_nothing(self):
        # Corporate link scanners fetch every URL in every inbound message.
        # A state-changing GET would opt out a whole company on the first send.
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.OPTED_IN
        )

    def test_get_names_the_group(self):
        response = self.client.get(self.url())

        self.assertContains(response, 'Family')

    def test_get_is_not_cached_or_indexed(self):
        response = self.client.get(self.url())

        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertIn('noindex', response['X-Robots-Tag'])

    # --- POST acts, once ----------------------------------------------------

    def test_post_stops_that_group(self):
        response = self.client.post(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )

    def test_and_leaves_every_other_group_alone(self):
        # The requirement this whole feature turns on.
        self.client.post(self.url())

        self.assertEqual(
            self.email_consent(self.bob_work), ChannelConsent.Status.OPTED_IN
        )

    def test_and_leaves_the_account_wide_switch_alone(self):
        self.client.post(self.url())

        self.assertTrue(MessagingSettings.objects.get(user=self.bob).email_enabled)

    def test_and_leaves_sms_alone(self):
        self.client.post(self.url())

        self.assertEqual(
            membership.consent_status(
                self.bob_family, ChannelConsent.Channel.SMS
            ),
            ChannelConsent.Status.OPTED_IN,
        )

    def test_it_records_when_they_said_no(self):
        self.client.post(self.url())

        consent = self.bob_family.channel_consents.get(
            channel=ChannelConsent.Channel.EMAIL
        )
        self.assertIsNotNone(consent.opted_out_at)

    def test_posting_twice_is_one_outcome(self):
        self.client.post(self.url())
        response = self.client.post(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )

    def test_the_fan_out_then_skips_them(self):
        self.client.post(self.url())

        send_to, skipped = outbound.recipients_for(
            create_message(self.family, self.alice, text='Dinner at 7 PM.')
        )

        self.assertEqual(send_to, [])
        self.assertEqual(
            dict((row.pk, reason) for row, reason in skipped)[self.bob_family.pk],
            'declined this group',
        )

    def test_somebody_who_left_can_still_stop_it(self):
        # The link outlives the membership - it is in a mailbox for as long as
        # the mail is.
        membership.leave(self.bob_family)

        response = self.client.post(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )

    # --- and is never an oracle ---------------------------------------------

    def test_a_tampered_token_looks_exactly_the_same(self):
        good = self.client.post(self.url())
        bad = self.client.post(
            reverse('mail-unsubscribe', args=['999999.0000000000000000abcd'])
        )

        self.assertEqual(bad.status_code, good.status_code)
        self.assertEqual(
            self.email_consent(self.bob_work), ChannelConsent.Status.OPTED_IN
        )

    def test_past_the_cap_it_still_answers(self):
        # Idempotent, so telling them it is done is true rather than a fiction.
        for _ in range(25):
            response = self.client.post(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, API_PUBLIC_URL=API_URL)
class GroupEmailFooterTests(TestCase):
    """Every group email carries a way out, in both parts."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)

    def send(self):
        with capture_sent() as sent:
            outbound.fan_out(
                create_message(self.group, self.alice, text='Dinner at 7 PM.')
            )
        return sent[0]

    def test_it_carries_one_click_unsubscribe(self):
        payload = self.send()

        self.assertTrue(
            payload['headers']['List-Unsubscribe'].startswith(f'<{API_URL}/mail/')
        )
        self.assertEqual(
            payload['headers']['List-Unsubscribe-Post'],
            'List-Unsubscribe=One-Click',
        )

    def test_the_link_is_ours_and_not_the_providers(self):
        # A provider-hosted unsubscribe lands on its ACCOUNT suppression list,
        # which really would stop every Loopd email to that address.
        payload = self.send()

        self.assertNotIn('resend', payload['headers']['List-Unsubscribe'].lower())
        self.assertNotIn('RESEND_UNSUBSCRIBE_URL', payload['html'])

    def test_it_carries_a_per_group_list_id(self):
        # Mail clients key their own unsubscribe control on the SENDER, and
        # every group shares one From address. This is what tells them apart.
        payload = self.send()

        self.assertEqual(
            payload['headers']['List-ID'], f'<group-{self.group.pk}.lists.loopdn.com>'
        )

    def test_two_members_get_different_links(self):
        carol = make_user('carol')
        self.group.add_participant(carol)

        with capture_sent() as sent:
            outbound.fan_out(
                create_message(self.group, self.alice, text='Dinner at 7 PM.')
            )

        links = {payload['headers']['List-Unsubscribe'] for payload in sent}
        self.assertEqual(len(links), 2)

    def test_the_text_part_carries_the_keyword_and_the_link(self):
        payload = self.send()

        self.assertIn(commands.KEYWORD, payload['text'])
        self.assertIn('/mail/unsubscribe/', payload['text'])

    def test_the_html_part_carries_them_too(self):
        payload = self.send()

        self.assertIn(commands.KEYWORD, payload['html'])
        self.assertIn('/mail/unsubscribe/', payload['html'])

    def test_the_footer_opens_with_a_signature_marker(self):
        # `-- ` with its trailing space is RFC 3676, and it is what every
        # quote-stripper cuts at - which is what stops our own footer being
        # read back as a command. Editors strip trailing whitespace on save,
        # so this asserts it survived.
        payload = self.send()

        self.assertIn('\n-- \n', payload['text'])

    def test_both_parts_say_other_groups_are_unaffected(self):
        # The only sentence that reaches somebody who pressed their mail
        # client's own Unsubscribe button, since the client never renders our
        # response.
        payload = self.send()

        self.assertIn('Other groups are not affected', payload['text'])
        self.assertIn('Other groups are not affected', payload['html'])

    @override_settings(MAIL_REPLY_DOMAIN='')
    def test_with_replies_off_the_link_stands_alone(self):
        payload = self.send()

        self.assertNotIn(commands.KEYWORD, payload['text'])
        self.assertIn('/mail/unsubscribe/', payload['text'])

    @override_settings(API_PUBLIC_URL='')
    def test_with_no_public_url_there_is_no_header(self):
        payload = self.send()

        self.assertNotIn('List-Unsubscribe', payload['headers'])
        self.assertIn('List-ID', payload['headers'])


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, API_PUBLIC_URL=API_URL)
class EmailKeywordTests(TestCase):
    """Replying STOP EMAILS, and everything that is not that."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.family = Conversation.start_group(self.alice, name='Family')
        self.work = Conversation.start_group(self.alice, name='Work')
        self.family.add_participant(self.bob)
        self.work.add_participant(self.bob)
        self.bob_family = Participant.objects.get(
            conversation=self.family, user=self.bob
        )
        self.bob_work = Participant.objects.get(
            conversation=self.work, user=self.bob
        )

    def reply(self, body, participant=None):
        row = participant or self.bob_family
        return inbound.handle_inbound(
            reply_address(row), 'bob@example.com', body,
            headers=authenticated(),
        )

    def email_consent(self, participant):
        return membership.consent_status(
            participant, ChannelConsent.Channel.EMAIL
        )

    def test_the_keyword_stops_that_group(self):
        result = self.reply('STOP EMAILS')

        self.assertEqual(result.action, 'email_declined')
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )

    def test_and_posts_nothing_into_the_group(self):
        self.reply('STOP EMAILS')

        self.assertEqual(self.family.messages.count(), 0)

    def test_and_leaves_other_groups_alone(self):
        self.reply('STOP EMAILS')

        self.assertEqual(
            self.email_consent(self.bob_work), ChannelConsent.Status.OPTED_IN
        )

    def test_the_shapes_a_person_actually_types(self):
        for body in ['stop emails', 'Stop Emails', 'STOP EMAILS.', ' STOP EMAILS ',
                     'STOP EMAIL', 'STOP  EMAILS', 'STOP\nEMAILS']:
            with self.subTest(body=body):
                self.assertTrue(commands.is_stop_emails(body))

    def test_the_words_inside_a_sentence_are_a_message(self):
        # The SMS lesson, held for email: a command is the whole message.
        result = self.reply('please stop emails about the party, thanks')

        self.assertEqual(result.action, 'posted')
        self.assertEqual(self.family.messages.count(), 1)
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.OPTED_IN
        )

    def test_our_own_footer_quoted_back_is_not_a_command(self):
        # Every group email prints the keyword, so most replies carry it in the
        # thread underneath. Stripping runs first, which is what stops us
        # reading our own instructions as an instruction.
        result = self.reply(
            "See you there.\n"
            "\n"
            "On Mon, Family via Loopd wrote:\n"
            "> Dinner at 7 PM.\n"
            ">\n"
            "> -- \n"
            "> You are getting this because you are in Family on Loopd.\n"
            "> To stop emails from this group, reply: STOP EMAILS\n"
            "> Other groups are not affected.\n"
        )

        self.assertEqual(result.action, 'posted')
        self.assertEqual(self.family.messages.first().text, 'See you there.')
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.OPTED_IN
        )

    def test_the_keyword_from_the_wrong_address_does_nothing(self):
        result = inbound.handle_inbound(
            reply_address(self.bob_family), 'mallory@example.com', 'STOP EMAILS',
            headers=authenticated(),
        )

        self.assertEqual(result.action, 'ignored_address_mismatch')
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.OPTED_IN
        )

    def test_a_second_one_is_idempotent(self):
        self.reply('STOP EMAILS')
        result = self.reply('STOP EMAILS')

        self.assertEqual(result.action, 'email_declined')
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )

    def test_afterwards_that_group_takes_no_replies_from_them(self):
        # Both directions: the group stops emailing them and stops taking
        # their email replies. They can still take part in the app.
        self.reply('STOP EMAILS')

        result = self.reply('On my way')

        self.assertEqual(result.action, 'ignored_email_declined')
        self.assertEqual(self.family.messages.count(), 0)

    def test_but_their_other_groups_still_do(self):
        self.reply('STOP EMAILS')

        result = self.reply('On my way', participant=self.bob_work)

        self.assertEqual(result.action, 'posted')
        self.assertEqual(self.work.messages.count(), 1)

    def test_the_keyword_still_works_with_email_off_everywhere(self):
        MessagingSettings.objects.filter(user=self.bob).update(email_enabled=False)

        result = self.reply('STOP EMAILS')

        self.assertEqual(result.action, 'email_declined')
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )


class UnsubscribeConfirmationTests(TransactionTestCase):
    """The one path that gets an email back, and the ones that do not.

    TransactionTestCase because the confirmation is queued with
    `transaction.on_commit` - deliberately, so nothing is sent about a
    preference that then rolls back.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )

    @override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, API_PUBLIC_URL=API_URL)
    def test_the_keyword_gets_one_email_back(self):
        # A member who replies and hears nothing does not know it worked, and
        # the next thing they do is press Report spam.
        with capture_sent() as sent:
            inbound.handle_inbound(
                reply_address(self.bob_row), 'bob@example.com', 'STOP EMAILS',
                headers=authenticated(),
            )

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['to'], ['bob@example.com'])
        self.assertIn('Family', sent[0]['subject'])

    @override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, API_PUBLIC_URL=API_URL)
    def test_and_it_does_not_offer_to_unsubscribe_again(self):
        with capture_sent() as sent:
            inbound.handle_inbound(
                reply_address(self.bob_row), 'bob@example.com', 'STOP EMAILS',
                headers=authenticated(),
            )

        self.assertNotIn('List-Unsubscribe', sent[0]['headers'])

    @override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, API_PUBLIC_URL=API_URL)
    def test_the_link_path_gets_no_email(self):
        # A mail arriving AFTER somebody pressed unsubscribe reads as being
        # ignored, and that is the click that becomes a spam complaint.
        token = unsubscribe_token_for(self.bob_row.pk)

        with capture_sent() as sent:
            self.client.post(reverse('mail-unsubscribe', args=[token]))

        self.assertEqual(sent, [])


class AuthenticationResultsTests(TestCase):
    """Reading the receiving server's verdict - RFC 8601 - and the trust rule."""

    def verdict(self, value):
        return authentication.verdict_from({'authentication-results': value})

    def test_a_full_pass_is_trusted(self):
        self.assertTrue(
            authentication.from_is_trusted(authenticated(), 'Bob <bob@example.com>')
        )

    def test_comments_are_ignored_however_deeply_nested(self):
        verdict = self.verdict(
            'mx.google.com; spf=pass (google.com: domain of bounce@example.com '
            'designates 1.2.3.4 as permitted sender) smtp.mailfrom=bounce@example.com; '
            'dkim=pass (nested (deep) comment) header.i=@example.com'
        )

        self.assertEqual(
            (verdict.spf, verdict.spf_domain, verdict.dkim, verdict.dkim_domain),
            ('pass', 'example.com', 'pass', 'example.com'),
        )

    def test_dmarc_pass_alone_is_enough(self):
        # DMARC is alignment by definition.
        self.assertTrue(
            self.verdict('mx; dmarc=pass header.from=example.com').trusts('bob@example.com')
        )

    def test_dkim_pass_must_be_for_the_from_domain(self):
        self.assertFalse(
            self.verdict('mx; dkim=pass header.d=mallory.net').trusts('bob@example.com')
        )

    def test_a_subdomain_is_aligned_either_way_round(self):
        self.assertTrue(
            self.verdict('mx; spf=pass smtp.mailfrom=bounce.example.com')
            .trusts('bob@example.com')
        )
        self.assertTrue(
            self.verdict('mx; dkim=pass header.d=example.com')
            .trusts('bob@mail.example.com')
        )

    def test_a_lookalike_is_not(self):
        self.assertFalse(
            self.verdict('mx; dkim=pass header.d=example.com.evil.net')
            .trusts('bob@example.com')
        )

    def test_anything_short_of_pass_is_not_trusted(self):
        for value in (
            'mx; dkim=fail header.d=example.com; spf=softfail '
            'smtp.mailfrom=example.com; dmarc=fail header.from=example.com',
            'mx; dkim=neutral header.d=example.com',
            'mx; none',
            '',
        ):
            with self.subTest(value=value):
                self.assertFalse(self.verdict(value).trusts('bob@example.com'))

    def test_no_header_is_not_trusted(self):
        # Fail closed - see the module docstring for why absent is not a pass.
        self.assertFalse(authentication.from_is_trusted({}, 'bob@example.com'))
        self.assertFalse(authentication.from_is_trusted(None, 'bob@example.com'))

    def test_only_the_newest_header_is_read(self):
        # The sender's own forged header sits UNDER the receiving server's.
        headers = {'authentication-results': [
            'mx.resend.com; dkim=fail header.d=example.com',
            'forged; dkim=pass header.d=example.com; dmarc=pass header.from=example.com',
        ]}

        self.assertFalse(authentication.from_is_trusted(headers, 'bob@example.com'))

    def test_one_valid_signature_among_broken_ones_counts(self):
        verdict = self.verdict(
            'mx; dkim=fail header.d=example.com; dkim=pass header.d=example.com'
        )

        self.assertEqual(verdict.dkim, 'pass')

    def test_an_address_with_no_domain_is_never_trusted(self):
        self.assertFalse(self.verdict('mx; dmarc=pass').trusts(''))
        self.assertFalse(self.verdict('mx; dmarc=pass').trusts('not-an-address'))


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN)
class InboundProviderTests(TestCase):
    """The two Resend shapes - the event and the fetched content - read into one."""

    def event(self, kind='email.received', **data):
        return provider.parse_event({'type': kind, 'data': data})

    def test_the_event_is_only_a_notice(self):
        event = self.event(
            email_id='abc', to=[f'reply+1.x@{REPLY_DOMAIN}'],
            **{'from': 'bob@example.com'}, subject='Re: Family',
        )

        self.assertEqual(event.kind, provider.INBOUND)
        self.assertEqual(
            (event.email_id, event.to_address, event.from_address),
            ('abc', f'reply+1.x@{REPLY_DOMAIN}', 'bob@example.com'),
        )

    def test_an_event_we_do_not_act_on_has_no_kind(self):
        for kind in ('email.opened', 'email.clicked', 'domain.created', ''):
            with self.subTest(kind=kind):
                self.assertEqual(self.event(kind, email_id='abc').kind, '')

    def test_a_receipt_is_read_with_the_providers_explanation(self):
        bounce = self.event(
            'email.bounced', email_id='sent-1',
            bounce={'type': 'Permanent', 'subType': 'Suppressed',
                    'message': 'mailbox unavailable'},
        )
        failed = self.event('email.failed', email_id='sent-2',
                            failed={'reason': 'reached_daily_quota'})
        soft = self.event('email.bounced', email_id='sent-3',
                          bounce={'type': 'Temporary', 'subType': 'MailboxFull'})

        self.assertEqual((bounce.kind, bounce.permanent), (provider.BOUNCED, True))
        self.assertEqual(bounce.detail, 'Permanent Suppressed mailbox unavailable')
        self.assertEqual((failed.kind, failed.detail), (provider.FAILED, 'reached_daily_quota'))
        self.assertEqual((soft.kind, soft.permanent), (provider.BOUNCED, False))
        self.assertEqual(self.event('email.complained', email_id='s').kind, provider.COMPLAINED)
        self.assertEqual(self.event('email.delivered', email_id='s').kind, provider.DELIVERED)
        self.assertEqual(self.event('email.suppressed', email_id='s').kind, provider.SUPPRESSED)

    def test_a_malformed_payload_is_nothing_to_do_rather_than_an_error(self):
        self.assertEqual(provider.parse_event({'type': 'email.bounced', 'data': 'junk'}).email_id, '')
        self.assertEqual(provider.parse_event({}).kind, '')

    def test_our_address_is_found_wherever_it_sits(self):
        ours = f'reply+1.x@{REPLY_DOMAIN}'

        self.assertEqual(
            self.event(email_id='a', to=['friend@example.com'], received_for=[ours]).to_address,
            ours,
        )
        self.assertEqual(
            self.event(email_id='a', to=[{'address': 'friend@example.com'}, ours]).to_address,
            ours,
        )
        # And when none is ours, the first To - so "not ours" has a name.
        self.assertEqual(
            self.event(email_id='a', to=['friend@example.com']).to_address,
            'friend@example.com',
        )

    def test_fetching_reads_the_content_and_lower_cases_the_headers(self):
        with mock.patch('mail.provider.fetch_json', return_value={
            'id': 'abc',
            'from': 'Bob <bob@example.com>',
            'to': [f'reply+1.x@{REPLY_DOMAIN}'],
            'subject': 'Re: New message in Family',
            'text': None,
            'html': '<p>On my way</p>',
            'headers': {
                'Authentication-Results': 'mx; dmarc=pass header.from=example.com',
                'From': 'Bob <bob@example.com>',
            },
        }) as fetch:
            received = provider.fetch_received('abc/../evil')

        # The id goes into a URL path, so it is escaped rather than trusted.
        self.assertIn('/emails/receiving/abc%2F..%2Fevil', fetch.call_args.args[0])
        self.assertEqual(received.body, 'On my way')
        self.assertEqual(received.subject, 'Re: New message in Family')
        self.assertEqual(
            received.headers['authentication-results'],
            'mx; dmarc=pass header.from=example.com',
        )
        self.assertEqual(received.from_address, 'Bob <bob@example.com>')

    def test_text_wins_over_html(self):
        received = provider.ReceivedEmail(
            email_id='e', from_address='b', to_address='x',
            text='plain', html='<b>rich</b>',
        )

        self.assertEqual(received.body, 'plain')

    def test_an_unconfigured_key_is_an_error_not_a_quiet_nothing(self):
        # Sending logs instead of sending when unconfigured. Fetching has
        # nothing to fall back on, so it says so.
        with mock.patch('utils.mail._is_configured', return_value=False):
            with self.assertRaises(EmailError):
                provider.fetch_received('abc')


class MailChecksTests(TestCase):
    """The deploy checks that stop a quietly wrong inbound configuration."""

    def problem_ids(self):
        from mail.checks import check_mail_configuration

        return {problem.id for problem in check_mail_configuration(None)}

    @override_settings(
        MAIL_REPLY_DOMAIN=REPLY_DOMAIN, RESEND_WEBHOOK_SECRET='whsec_x',
        RESEND_API_KEY='re_x', MAIL_INBOUND_REQUIRE_AUTHENTICATION=True,
    )
    def test_a_complete_inbound_configuration_is_clean(self):
        self.assertFalse(
            {'mail.E001', 'mail.E003', 'mail.E004'} & self.problem_ids()
        )

    @override_settings(
        MAIL_REPLY_DOMAIN=REPLY_DOMAIN, RESEND_WEBHOOK_SECRET='whsec_x',
        RESEND_API_KEY='re_x', MAIL_INBOUND_REQUIRE_AUTHENTICATION=False,
    )
    def test_replies_on_with_authentication_off_blocks_the_deploy(self):
        self.assertIn('mail.E003', self.problem_ids())

    @override_settings(
        MAIL_REPLY_DOMAIN=REPLY_DOMAIN, RESEND_WEBHOOK_SECRET='whsec_x',
        RESEND_API_KEY='', MAIL_INBOUND_REQUIRE_AUTHENTICATION=True,
    )
    def test_replies_on_with_no_api_key_blocks_the_deploy(self):
        # The event has no body; without the key nothing can ever be fetched.
        self.assertIn('mail.E004', self.problem_ids())

    @override_settings(
        MAIL_REPLY_DOMAIN='', RESEND_API_KEY='',
        MAIL_INBOUND_REQUIRE_AUTHENTICATION=False,
    )
    def test_with_replies_off_neither_applies(self):
        self.assertFalse({'mail.E003', 'mail.E004'} & self.problem_ids())


class AutoReplyTests(TestCase):
    """A vacation responder passes every identity check. It must still not post."""

    def test_rfc_3834_auto_submitted_is_a_machine_unless_it_says_no(self):
        for value in ('auto-replied', 'auto-generated', 'auto-notified', 'Auto-Replied; owner-notify'):
            with self.subTest(value=value):
                self.assertTrue(autoreply.is_automatic({'auto-submitted': value}))
        self.assertFalse(autoreply.is_automatic({'auto-submitted': 'no'}))

    def test_the_older_headers_count_by_being_present(self):
        for name in ('x-autoreply', 'x-autorespond', 'x-auto-response-suppress'):
            with self.subTest(name=name):
                self.assertTrue(autoreply.is_automatic({name: 'All'}))

    def test_precedence(self):
        for value, automatic in (('bulk', True), ('junk', True), ('auto_reply', True),
                                 ('list', False), ('first-class', False)):
            with self.subTest(value=value):
                self.assertEqual(autoreply.is_automatic({'precedence': value}), automatic)

    def test_a_subject_that_says_so_is_the_last_resort(self):
        for subject in ('Automatic reply: New message in Family', 'Re: Out of Office',
                        'AutoReply: Dinner', 'Auto-Reply', 'OOO until Monday'):
            with self.subTest(subject=subject):
                self.assertTrue(autoreply.is_automatic({}, subject))
        for subject in ('Re: New message in Family', "I'll be there", 'Autos for sale'):
            with self.subTest(subject=subject):
                self.assertFalse(autoreply.is_automatic({}, subject))

    def test_a_header_given_as_a_list_reads_its_first_value(self):
        self.assertTrue(autoreply.is_automatic({'auto-submitted': ['auto-replied']}))

    def test_an_ordinary_reply_is_not_automatic(self):
        self.assertFalse(autoreply.is_automatic(authenticated(), 'Re: Dinner'))
        self.assertFalse(autoreply.is_automatic(None))


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN)
class AutoReplyIsNotPostedTests(TestCase):
    """The refusal, where it lives: after identity, before the words are read."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(conversation=self.group, user=self.bob)

    def reply(self, **kwargs):
        return inbound.handle_inbound(
            reply_address(self.bob_row), 'bob@example.com', 'I am away until Monday.',
            **{'headers': authenticated(), **kwargs},
        )

    def test_a_responder_from_the_members_own_address_is_dropped(self):
        result = self.reply(headers={**authenticated(), 'auto-submitted': 'auto-replied'})

        self.assertEqual(result.action, 'ignored_auto_reply')
        self.assertEqual(self.group.messages.count(), 0)

    def test_a_responder_that_only_says_so_in_the_subject_is_dropped(self):
        result = self.reply(subject='Automatic reply: New message in Family')

        self.assertEqual(result.action, 'ignored_auto_reply')

    def test_the_same_words_typed_by_a_person_still_post(self):
        self.assertEqual(self.reply(subject='Re: New message in Family').action, 'posted')


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN)
class EmailMaintenanceTests(TestCase):
    """A failed email is no longer terminal - and a retry re-asks the question."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(conversation=self.group, user=self.bob)

    def a_failed_delivery(self, *, attempts=None, reason=None, permanent=False):
        """A real FAILED row, produced by the real fan-out meeting a provider error."""
        message = create_message(self.group, self.alice, text='Dinner at 7')
        with mock.patch('mail.outbound.send_templated_email',
                        side_effect=EmailError('down', permanent=permanent)):
            outbound.fan_out(message)
        row = EmailDelivery.objects.get(message=message, participant=self.bob_row)
        if attempts is not None or reason is not None:
            row.attempts = row.attempts if attempts is None else attempts
            row.reason = row.reason if reason is None else reason
            row.save(update_fields=['attempts', 'reason'])
        return row

    # --- what the fan-out writes --------------------------------------------

    def test_a_provider_outage_and_a_provider_rejection_are_told_apart(self):
        outage = self.a_failed_delivery()
        rejection = self.a_failed_delivery(permanent=True)

        self.assertEqual((outage.status, outage.reason, outage.attempts),
                         ('failed', 'provider unavailable', 1))
        self.assertEqual(rejection.reason, 'provider rejected the message')

    # --- retry ---------------------------------------------------------------

    def test_a_failed_delivery_is_tried_again(self):
        row = self.a_failed_delivery()

        with capture_sent() as sent:
            self.assertEqual(maintenance.retry_failed_deliveries(), 1)

        row.refresh_from_db()
        self.assertEqual(row.status, EmailDelivery.Status.SENT)
        self.assertEqual(row.reason, '')
        self.assertEqual(row.attempts, 2)
        self.assertEqual(sent[0]['to'], ['bob@example.com'])

    def test_retrying_stops_after_a_few_goes(self):
        row = self.a_failed_delivery(attempts=settings.MAIL_MAX_DELIVERY_ATTEMPTS)

        with capture_sent() as sent:
            self.assertEqual(maintenance.retry_failed_deliveries(), 0)

        row.refresh_from_db()
        self.assertEqual(row.status, EmailDelivery.Status.FAILED)
        self.assertEqual(sent, [])

    def test_a_rejection_is_never_retried(self):
        # 401, 403, 422: wrong until somebody changes it. Trying again asks the
        # same question of the same answer.
        self.a_failed_delivery(permanent=True)

        with capture_sent() as sent:
            self.assertEqual(maintenance.retry_failed_deliveries(), 0)
        self.assertEqual(sent, [])

    def test_a_decision_is_never_retried(self):
        # SKIPPED, BOUNCED and COMPLAINED are all answers, not failures.
        for status_ in (EmailDelivery.Status.SKIPPED, EmailDelivery.Status.BOUNCED,
                        EmailDelivery.Status.COMPLAINED):
            with self.subTest(status=status_):
                row = self.a_failed_delivery()
                row.status = status_
                row.save(update_fields=['status'])

                self.assertEqual(maintenance.retry_failed_deliveries(), 0)

    def test_a_retry_is_dropped_if_they_said_stop_emails_meanwhile(self):
        # The window is up to six hours. PERMANENT_REASONS cannot help: it
        # excludes rows that were ALREADY a refusal, not ones that became one.
        row = self.a_failed_delivery()
        inbound.handle_inbound(
            reply_address(self.bob_row), 'bob@example.com', commands.KEYWORD,
            headers=authenticated(),
        )

        with capture_sent() as sent:
            self.assertEqual(maintenance.retry_failed_deliveries(), 0)

        row.refresh_from_db()
        self.assertEqual((row.status, row.reason), ('skipped', 'declined this group'))
        self.assertEqual(sent, [])
        # And SKIPPED is self-limiting: the sweep never picks it up again.
        self.assertEqual(maintenance.retry_failed_deliveries(), 0)

    def assert_dropped_as(self, row, reason):
        with capture_sent() as sent:
            self.assertEqual(maintenance.retry_failed_deliveries(), 0)
        row.refresh_from_db()
        self.assertEqual((row.status, row.reason), ('skipped', reason))
        self.assertEqual(sent, [])

    def test_a_retry_is_dropped_if_they_muted_meanwhile(self):
        row = self.a_failed_delivery()
        membership.set_muted(self.bob_row, True)

        self.assert_dropped_as(row, 'muted')

    def test_a_retry_is_dropped_if_they_left_meanwhile(self):
        row = self.a_failed_delivery()
        membership.leave(self.bob_row)

        self.assert_dropped_as(row, 'not an active member')

    def test_a_retry_is_dropped_if_they_turned_email_off_account_wide(self):
        # The switch Settings > Messaging shows, read by email's own
        # address question - the third rung of the retry check, after the
        # ladder and the block.
        row = self.a_failed_delivery()
        self.bob.messaging_settings.email_enabled = False
        self.bob.messaging_settings.save(update_fields=['email_enabled'])

        self.assert_dropped_as(row, 'email disabled account-wide')

    def test_a_retry_is_dropped_if_they_blocked_the_sender_meanwhile(self):
        from chat.delivery import BLOCKED_REASON
        from user.blocks import block

        row = self.a_failed_delivery()
        block(self.bob, user=self.alice)

        self.assertEqual(maintenance.retry_failed_deliveries(), 0)
        row.refresh_from_db()
        self.assertEqual((row.status, row.reason), ('skipped', BLOCKED_REASON))

    def test_a_row_claimed_by_one_worker_is_not_sent_by_another(self):
        row = self.a_failed_delivery()

        with capture_sent() as sent:
            first = outbound.retry_delivery(row)
            second = outbound.retry_delivery(row)

        self.assertEqual((first, second), (True, False))
        self.assertEqual(len(sent), 1)

    def test_a_retry_that_fails_again_is_still_a_failure_with_a_reason(self):
        row = self.a_failed_delivery()

        with mock.patch('mail.outbound.send_templated_email',
                        side_effect=EmailError('still down')):
            self.assertEqual(maintenance.retry_failed_deliveries(), 0)

        row.refresh_from_db()
        self.assertEqual((row.status, row.reason, row.attempts),
                         ('failed', 'provider unavailable', 2))

    # --- stalled and old -----------------------------------------------------

    def test_a_stalled_send_is_failed_and_then_retried(self):
        row = self.a_failed_delivery()
        long_ago = timezone.now() - timedelta(minutes=settings.MAIL_STALLED_AFTER_MINUTES + 1)
        EmailDelivery.objects.filter(pk=row.pk).update(
            status=EmailDelivery.Status.SENDING, updated_at=long_ago,
        )

        self.assertEqual(maintenance.requeue_stalled_deliveries(), 1)
        row.refresh_from_db()
        self.assertEqual((row.status, row.reason), ('failed', 'no answer from the sender process'))

        with capture_sent():
            self.assertEqual(maintenance.retry_failed_deliveries(), 1)

    def test_a_send_still_in_progress_is_left_alone(self):
        row = self.a_failed_delivery()
        EmailDelivery.objects.filter(pk=row.pk).update(status=EmailDelivery.Status.SENDING)

        self.assertEqual(maintenance.requeue_stalled_deliveries(), 0)

    def test_old_rows_are_purged(self):
        row = self.a_failed_delivery()
        EmailDelivery.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(days=settings.MAIL_RETENTION_DAYS + 1)
        )
        EmailWebhookEvent.objects.create(provider_event_id='old')
        EmailWebhookEvent.objects.filter(provider_event_id='old').update(
            received_at=timezone.now() - timedelta(days=settings.MAIL_RETENTION_DAYS + 1)
        )

        self.assertEqual(maintenance.purge_old_deliveries(), (1, 1))
        self.assertFalse(EmailDelivery.objects.filter(pk=row.pk).exists())


@override_settings(MAIL_REPLY_DOMAIN=REPLY_DOMAIN, RESEND_WEBHOOK_SECRET='')
class ReceiptTests(APITestCase):
    """What the provider says afterwards is written down - and two of them act."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(conversation=self.group, user=self.bob)
        self.url = reverse('mail:inbound-webhook')

        message = create_message(self.group, self.alice, text='Dinner at 7')
        with capture_sent():
            outbound.fan_out(message)
        self.row = EmailDelivery.objects.get(message=message, participant=self.bob_row)
        self.assertEqual(self.row.provider_message_id, 'resend-1')

    def receipt(self, kind, event_id='evt-r1', **extra):
        payload = {'type': kind, 'data': {'email_id': 'resend-1', 'to': ['bob@example.com'], **extra}}
        return self.client.post(
            self.url, data=json.dumps(payload), content_type='application/json',
            HTTP_SVIX_ID=event_id,
        )

    def email_switch(self):
        return MessagingSettings.objects.get(user=self.bob).email_enabled

    def assertEmailTurnedOffFor(self, reason):
        switches = MessagingSettings.objects.get(user=self.bob)
        self.assertFalse(switches.email_enabled)
        self.assertIsNotNone(switches.email_opt_out_at)
        self.assertEqual(switches.email_opt_out_reason, reason)

    def test_delivered(self):
        response = self.receipt('email.delivered')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, EmailDelivery.Status.DELIVERED)
        self.assertIsNotNone(self.row.delivered_at)

    def test_a_permanent_bounce_switches_the_account_off(self):
        self.receipt('email.bounced', bounce={'type': 'Permanent', 'subType': 'General',
                                               'message': 'mailbox unavailable'})

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, EmailDelivery.Status.BOUNCED)
        self.assertEqual(self.row.reason, 'bounced: Permanent General mailbox unavailable')
        self.assertEmailTurnedOffFor(MessagingSettings.EmailOptOutReason.BOUNCE)

    def test_a_temporary_bounce_changes_only_the_row(self):
        self.receipt('email.bounced', bounce={'type': 'Temporary', 'subType': 'MailboxFull'})

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, EmailDelivery.Status.BOUNCED)
        self.assertTrue(self.email_switch())

    def test_a_complaint_switches_the_account_off_and_the_next_message_skips_them(self):
        self.receipt('email.complained')

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, EmailDelivery.Status.COMPLAINED)
        self.assertEmailTurnedOffFor(MessagingSettings.EmailOptOutReason.COMPLAINT)

        # The switch is the same one Settings > Messaging shows, and the
        # fan-out reads it - so nothing more reaches them until they turn it on.
        _, skipped = outbound.recipients_for(
            create_message(self.group, self.alice, text='Anyone?')
        )
        self.assertEqual(dict((r.pk, why) for r, why in skipped)[self.bob_row.pk],
                         'email disabled account-wide')

    def test_a_complaint_sticks_even_for_an_account_with_no_settings_row(self):
        # Every account gets its row at sign-up, but an opt-out must not lean
        # on that: with the row missing, the complaint creates it already off.
        MessagingSettings.objects.filter(user=self.bob).delete()

        self.receipt('email.complained')

        self.assertEmailTurnedOffFor(MessagingSettings.EmailOptOutReason.COMPLAINT)

    def test_an_account_already_off_keeps_the_first_reason(self):
        # They turned it off themselves; a later complaint about an email sent
        # before that does not rewrite why.
        switches = MessagingSettings.objects.get(user=self.bob)
        switches.save(update_fields=switches.switch_email(
            False, reason=MessagingSettings.EmailOptOutReason.PERSON
        ))

        self.receipt('email.complained')

        self.assertEmailTurnedOffFor(MessagingSettings.EmailOptOutReason.PERSON)

    def test_a_suppressed_send_is_a_permanent_failure(self):
        self.receipt('email.suppressed',
                     suppressed={'type': 'OnAccountSuppressionList', 'message': 'on the list'})

        self.row.refresh_from_db()
        self.assertEqual((self.row.status, self.row.reason),
                         ('failed', 'suppressed by the provider'))
        self.assertIn(self.row.reason, maintenance.PERMANENT_REASONS)

    def test_a_late_failure_is_left_for_the_retry_sweep(self):
        self.receipt('email.failed', failed={'reason': 'reached_daily_quota'})

        self.row.refresh_from_db()
        self.assertEqual((self.row.status, self.row.reason),
                         ('failed', 'provider failed: reached_daily_quota'))
        with capture_sent():
            self.assertEqual(maintenance.retry_failed_deliveries(), 1)

    def test_a_receipt_for_mail_we_do_not_know_changes_nothing(self):
        payload = {'type': 'email.bounced',
                   'data': {'email_id': 'somebody-elses', 'bounce': {'type': 'Permanent'}}}
        response = self.client.post(
            self.url, data=json.dumps(payload), content_type='application/json',
            HTTP_SVIX_ID='evt-x',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, EmailDelivery.Status.SENT)
        self.assertTrue(self.email_switch())

    def test_the_same_receipt_twice_is_applied_once(self):
        self.receipt('email.complained', event_id='evt-same')
        switches = MessagingSettings.objects.get(user=self.bob)
        switches.email_enabled = True
        switches.save(update_fields=['email_enabled'])

        response = self.receipt('email.complained', event_id='evt-same')

        self.assertEqual(response.json().get('duplicate'), True)
        self.assertTrue(self.email_switch())

    def test_bounced_and_complained_rows_are_never_retried(self):
        self.receipt('email.bounced', bounce={'type': 'Permanent'})
        with capture_sent() as sent:
            self.assertEqual(maintenance.retry_failed_deliveries(), 0)
        self.assertEqual(sent, [])


class EmailFailureAlertTests(TestCase):
    """Giving up on an email tells whoever wrote the message.

    The mirror of the retry sweep: what that sweep will never pick up again is
    exactly what this one announces. One alert per message, however many
    addresses failed - see notification/delivery_alerts.py.
    """

    def setUp(self):
        from chat.models import Conversation

        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.message = create_message(self.group, self.alice, text='Dinner at 7')

    def failed(self, **kwargs):
        from mail.models import EmailDelivery

        values = {
            'direction': EmailDelivery.Direction.OUTBOUND,
            'conversation': self.group,
            'message': self.message,
            'status': EmailDelivery.Status.FAILED,
            'reason': 'provider rejected the message',
            'attempts': 1,
        }
        values.update(kwargs)
        return EmailDelivery.objects.create(**values)

    def announce(self):
        from mail.maintenance import announce_failed_deliveries

        return announce_failed_deliveries()

    def alerts(self):
        from notification.models import Notification

        return Notification.objects.filter(
            type=Notification.Type.EMAIL_SEND_FAILED
        )

    def test_a_permanent_failure_is_announced(self):
        self.failed()

        self.assertEqual(self.announce(), 1)
        self.assertEqual(self.alerts().get().data['count'], 1)

    def test_several_addresses_are_one_alert_with_a_count(self):
        self.failed()
        self.failed()
        self.failed()

        self.announce()

        self.assertEqual(self.alerts().count(), 1)
        self.assertEqual(self.alerts().get().data['count'], 3)

    def test_a_failure_still_worth_retrying_says_nothing(self):
        """The retry sweep fixes most of these minutes later."""
        self.failed(reason='provider unavailable', attempts=1)

        self.assertEqual(self.announce(), 0)
        self.assertEqual(self.alerts().count(), 0)

    def test_spent_attempts_are_announced(self):
        self.failed(
            reason='provider unavailable',
            attempts=settings.MAIL_MAX_DELIVERY_ATTEMPTS,
        )

        self.assertEqual(self.announce(), 1)

    def test_a_bounce_is_announced(self):
        """The provider's own verdict; no retry can change it."""
        from mail.models import EmailDelivery

        self.failed(status=EmailDelivery.Status.BOUNCED, reason='bounced')

        self.assertEqual(self.announce(), 1)

    def test_a_skip_is_not_a_failure(self):
        """Muted, opted out, no address - decisions, not failures."""
        from mail.models import EmailDelivery

        self.failed(status=EmailDelivery.Status.SKIPPED, reason='muted')

        self.assertEqual(self.announce(), 0)

    def test_sweeping_twice_does_not_announce_twice(self):
        self.failed()

        self.announce()
        self.announce()

        self.assertEqual(self.alerts().count(), 1)
