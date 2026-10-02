"""Tests for conversations, membership, messages and the realtime layer."""
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from channels.db import database_sync_to_async
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APITransactionTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from chat import delivery, membership
from user import blocks
from sms import inbound
from chat.mentions import set_mentions
from chat.models import (
    ChannelConsent,
    Attachment,
    BlockedFromConversation,
    Conversation,
    GroupCategory,
    Message,
    MessageReaction,
    Participant,
    PostingNotAllowed,
    REACTION_EMOJI,
)
from chat.serializers import ConversationSerializer, MessageSerializer
from chat.services import broadcast_message_updated, create_message
from user.tests import EnvelopeAssertions, make_image_upload

def grant_sms(participant, status):
    """Set a member's SMS consent, the way production does.

    Through membership.set_channel_consent rather than by writing the row, so
    a test sets consent by the same route the app does and cannot drift from
    it - which is the whole reason that entry point exists.
    """
    from chat import membership

    return membership.set_channel_consent(
        participant, ChannelConsent.Channel.SMS, status
    )


def sms_consent_of(participant):
    """A member's SMS consent status, or None if there is no record."""
    from chat import membership

    return membership.consent_status(participant, ChannelConsent.Channel.SMS)


MEDIA_ROOT = tempfile.mkdtemp(prefix='loopd-chat-media-')


def members_of(client, conversation_id):
    """The active member list, fetched from its own paginated endpoint.

    Conversation payloads deliberately do not carry `participants`: the list is
    unbounded and every row nests a user and a profile, so a group of two
    hundred would put two hundred profiles in every fetch of every
    conversation.

    `page_size` is raised past the default of 10 so a test asserting on the
    whole membership is not silently reading only the first page.
    """
    response = client.get(
        reverse('chat:conversation-participants', args=[conversation_id]),
        {'page_size': 100},
    )
    return response.json()['data']['results']


def make_user(username, phone=None, password='a-strong-passphrase-42'):
    user = User.objects.create_user(
        username=username, email=f'{username}@example.com', password=password
    )
    if phone:
        # Verified in the same save, which is what Profile.save() treats as the
        # verification flow rather than a number change. Unverified numbers are
        # deliberately not resolvable by find_user, so a fixture that skipped
        # this would make every lookup-by-number test fail for the wrong reason.
        user.profile.phone_number = phone
        user.profile.phone_no_verified = True
        user.profile.save()
    return user


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ChatAPITests(EnvelopeAssertions, APITestCase):
    def setUp(self):
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.carol = make_user('carol', '+639170000003')
        self.client.force_authenticate(self.alice)

    # -- starting conversations -------------------------------------------

    def test_start_direct_by_username(self):
        data = self.assertSuccess(
            self.client.post(reverse('chat:conversation-direct'), {'identifier': 'bob'})
        )
        self.assertEqual(data['kind'], 'direct')
        self.assertEqual(len(members_of(self.client, data['id'])), 2)

    def test_start_direct_by_phone_number(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-direct'), {'identifier': '+639170000002'}
            )
        )
        usernames = {
            p['user']['username'] for p in members_of(self.client, data['id'])
        }
        self.assertEqual(usernames, {'alice', 'bob'})

    def a_discoverable_group(self, name, join_mode, **kwargs):
        group = Conversation.start_group(self.alice, name, **kwargs)
        group.join_mode = join_mode
        group.save(update_fields=['join_mode'])
        return group

    def search(self, term, as_user=None):
        self.client.force_authenticate(as_user or self.bob)
        return self.assertSuccess(
            self.client.get(reverse('chat:conversation-search'), {'q': term})
        )

    def test_search_conversations_returns_joinable_groups_only(self):
        open_group = self.a_discoverable_group(
            'Weekend Plans', Conversation.JoinMode.OPEN,
            description='Pickleball in CDO',
        )
        open_group.mode = Conversation.Mode.BROADCAST
        open_group.save(update_fields=['mode'])
        # Invite-only is not discoverable: listing it would announce a group to
        # somebody who can never join it.
        self.a_discoverable_group('Weekend Private', Conversation.JoinMode.INVITE)

        data = self.search('weekend')

        self.assertEqual(len(data['results']), 1)
        self.assertEqual(
            data['results'][0],
            {
                'id': open_group.id,
                'name': 'Weekend Plans',
                'description': 'Pickleball in CDO',
                'image_url': None,
                'member_count': 1,
                'join_mode': 'open',
                'mode': 'broadcast',
            },
        )

    def test_code_and_approval_groups_are_discoverable(self):
        self.a_discoverable_group('Weekend Code', Conversation.JoinMode.CODE)
        self.a_discoverable_group('Weekend Approval', Conversation.JoinMode.APPROVAL)

        data = self.search('weekend')

        self.assertEqual(
            {row['join_mode'] for row in data['results']}, {'code', 'approval'}
        )

    def test_a_group_you_are_already_in_is_not_a_result(self):
        """It is in the sidebar. Offering "Join" for it can only confuse."""
        group = self.a_discoverable_group('Weekend Plans', Conversation.JoinMode.OPEN)

        self.assertEqual(len(self.search('weekend')['results']), 1)

        group.add_participant(self.bob)
        self.assertEqual(self.search('weekend')['results'], [])

    def test_the_creator_never_sees_their_own_group(self):
        self.a_discoverable_group('Weekend Plans', Conversation.JoinMode.OPEN)
        self.assertEqual(self.search('weekend', as_user=self.alice)['results'], [])

    def test_leaving_makes_it_discoverable_again(self):
        # LEFT is not ACTIVE, so the door reopens - which is what makes the
        # exclusion a statement about membership rather than about history.
        group = self.a_discoverable_group('Weekend Plans', Conversation.JoinMode.OPEN)
        row = group.add_participant(self.bob)
        self.assertEqual(self.search('weekend')['results'], [])

        row.status = Participant.Status.LEFT
        row.save(update_fields=['status'])
        self.assertEqual(len(self.search('weekend')['results']), 1)

    def test_member_count_counts_only_active_members(self):
        group = self.a_discoverable_group('Weekend Plans', Conversation.JoinMode.OPEN)
        row = group.add_participant(self.carol)
        self.assertEqual(self.search('weekend')['results'][0]['member_count'], 2)

        row.status = Participant.Status.REMOVED
        row.save(update_fields=['status'])
        self.assertEqual(self.search('weekend')['results'][0]['member_count'], 1)

    def test_the_same_pair_never_gets_two_conversations(self):
        first = self.assertSuccess(
            self.client.post(reverse('chat:conversation-direct'), {'identifier': 'bob'})
        )
        # Bob starting it from his side must land in the same conversation.
        self.client.force_authenticate(self.bob)
        second = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-direct'), {'identifier': 'alice'}
            )
        )
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(Conversation.objects.count(), 1)

    def test_cannot_start_a_direct_conversation_with_yourself(self):
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-direct'), {'identifier': 'alice'}
            )
        )

    def test_unknown_identifier_is_rejected(self):
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-direct'), {'identifier': 'nobody'}
            )
        )

    def test_unverified_users_cannot_be_found(self):
        User.objects.filter(pk=self.bob.pk).update(is_active=False)
        self.assertFailure(
            self.client.post(reverse('chat:conversation-direct'), {'identifier': 'bob'})
        )

    # -- groups ------------------------------------------------------------

    def test_creating_a_group_makes_me_its_admin(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'), {'name': 'Weekend'}, format='json'
            ),
            status.HTTP_201_CREATED,
        )
        self.assertEqual(data['kind'], 'group')
        self.assertEqual(data['name'], 'Weekend')

        participant = Participant.objects.get(conversation_id=data['id'], user=self.alice)
        self.assertEqual(participant.role, Participant.Role.ADMIN)

    def test_group_can_be_created_with_members(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'members': ['bob', '+639170000003']},
                format='json',
            ),
            status.HTTP_201_CREATED,
        )
        usernames = {
            p['user']['username'] for p in members_of(self.client, data['id'])
        }
        self.assertEqual(usernames, {'alice', 'bob', 'carol'})

    def test_group_creation_reports_unknown_members(self):
        body = self.assertFailure(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'members': ['bob', 'ghost']},
                format='json',
            )
        )
        self.assertIn('ghost', str(body['data']))

    # -- a group's description and image -----------------------------------

    def test_a_group_can_be_created_with_a_description(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'description': 'Palawan, March.'},
                format='json',
            ),
            status.HTTP_201_CREATED,
        )
        self.assertEqual(data['description'], 'Palawan, March.')

    def test_a_group_created_without_one_has_a_blank_description(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'), {'name': 'Trip'}, format='json'
            ),
            status.HTTP_201_CREATED,
        )
        self.assertEqual(data['description'], '')
        self.assertIsNone(data['image_url'])

    def test_a_description_longer_than_the_column_is_rejected(self):
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'description': 'x' * 281},
                format='json',
            )
        )

    def test_a_group_can_be_created_with_an_image(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'image': make_image_upload('group.png')},
                format='multipart',
            ),
            status.HTTP_201_CREATED,
        )
        self.assertTrue(data['image_url'].startswith('http'))

        conversation = Conversation.objects.get(pk=data['id'])
        # Filed under the conversation, not under the literal string 'None':
        # a FileField is written before the INSERT, so start_group has to
        # attach the image in a second save to have a pk to file it by.
        self.assertTrue(
            conversation.image.name.startswith(f'chat/{conversation.pk}/group/'),
            conversation.image.name,
        )

    def test_a_failed_image_upload_leaves_no_conversation_behind(self):
        # The storage write is a network call to S3 in staging and production,
        # and it sits between the INSERT and add_participant. Without the
        # atomic block on start_group a timeout would commit a conversation
        # with no participants - and access control here IS the queryset, so
        # such a row is invisible to its own creator and unreachable forever.
        with mock.patch.object(
            default_storage, '_save', side_effect=OSError('S3 timed out')
        ):
            # loopd/exceptions.py turns the unhandled error into a 500 inside
            # the envelope, so nothing escapes the client.
            self.assertFailure(
                self.client.post(
                    reverse('chat:conversation-group'),
                    {'name': 'Doomed', 'image': make_image_upload('group.png')},
                    format='multipart',
                ),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        self.assertFalse(Conversation.objects.filter(name='Doomed').exists())
        self.assertFalse(
            Participant.objects.filter(conversation__name='Doomed').exists()
        )

    def test_the_uploaded_filename_is_not_the_stored_one(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'image': make_image_upload('IMG_0001.png')},
                format='multipart',
            ),
            status.HTTP_201_CREATED,
        )
        conversation = Conversation.objects.get(pk=data['id'])
        self.assertNotIn('IMG_0001', conversation.image.name)
        self.assertTrue(conversation.image.name.endswith('.png'))

    @override_settings(MAX_GROUP_IMAGE_UPLOAD_BYTES=10)
    def test_an_oversized_group_image_is_rejected(self):
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'image': make_image_upload('group.png')},
                format='multipart',
            )
        )
        self.assertFalse(Conversation.objects.filter(name='Trip').exists())

    def test_a_file_that_is_not_an_image_is_rejected(self):
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-group'),
                {
                    'name': 'Trip',
                    'image': SimpleUploadedFile(
                        'notes.png', b'not an image', content_type='image/png'
                    ),
                },
                format='multipart',
            )
        )

    def test_the_group_photo_is_visible_to_every_member(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'),
                {
                    'name': 'Trip',
                    'description': 'Palawan, March.',
                    'members': ['bob'],
                    'image': make_image_upload('group.png'),
                },
                format='multipart',
            ),
            status.HTTP_201_CREATED,
        )

        self.client.force_authenticate(self.bob)
        seen = self.assertSuccess(
            self.client.get(reverse('chat:conversation-detail', args=[data['id']]))
        )
        self.assertEqual(seen['description'], 'Palawan, March.')
        self.assertEqual(seen['image_url'], data['image_url'])

    def test_a_direct_conversation_carries_neither(self):
        data = self.assertSuccess(
            self.client.post(reverse('chat:conversation-direct'), {'identifier': 'bob'})
        )
        self.assertEqual(data['description'], '')
        self.assertIsNone(data['image_url'])

    def test_a_broadcast_payload_carries_the_image_as_a_relative_path(self):
        # broadcast_added_to_conversation serialises with no request, so there
        # is no host to build an absolute URL from. Clients resolve it against
        # the API host they already know - the same rule as an attachment.
        conversation = Conversation.start_group(
            self.alice, 'Trip', image=make_image_upload('group.png')
        )
        payload = ConversationSerializer(conversation).data
        self.assertTrue(payload['image_url'].startswith('/'), payload['image_url'])

    # -- editing a group ---------------------------------------------------

    def patch_settings(self, conversation, payload, multipart=False):
        """PATCH the group settings route.

        Multipart PATCH needs encoding by hand: the test client's patch()
        defaults to application/octet-stream - only post() defaults to
        multipart - which DRF answers with a 415.
        """
        url = reverse('chat:conversation-settings', args=[conversation.id])
        if multipart:
            return self.client.patch(
                url,
                encode_multipart(BOUNDARY, payload),
                content_type=MULTIPART_CONTENT,
            )
        return self.client.patch(url, payload, format='json')

    def test_an_admin_can_rename_a_group_and_edit_its_description(self):
        group = Conversation.start_group(self.alice, 'Trip', description='Draft.')
        data = self.assertSuccess(
            self.patch_settings(group, {'name': 'Palawan', 'description': 'Booked.'})
        )
        self.assertEqual(data['name'], 'Palawan')
        self.assertEqual(data['description'], 'Booked.')

    def test_editing_one_field_leaves_the_others_alone(self):
        group = Conversation.start_group(self.alice, 'Trip', description='Booked.')
        data = self.assertSuccess(self.patch_settings(group, {'name': 'Palawan'}))
        self.assertEqual(data['description'], 'Booked.')

    def test_an_admin_can_replace_the_group_photo(self):
        group = Conversation.start_group(
            self.alice, 'Trip', image=make_image_upload('first.png')
        )
        first = group.image.name

        data = self.assertSuccess(
            self.patch_settings(
                group, {'image': make_image_upload('second.png')}, multipart=True
            )
        )
        self.assertIsNotNone(data['image_url'])

        group.refresh_from_db()
        self.assertNotEqual(group.image.name, first)

    def test_an_admin_can_remove_the_group_photo(self):
        group = Conversation.start_group(
            self.alice, 'Trip', image=make_image_upload('group.png')
        )
        data = self.assertSuccess(
            self.patch_settings(group, {'image': ''}, multipart=True)
        )
        self.assertIsNone(data['image_url'])

        group.refresh_from_db()
        self.assertFalse(group.image)

    @override_settings(MAX_GROUP_IMAGE_UPLOAD_BYTES=10)
    def test_an_oversized_replacement_photo_is_rejected(self):
        group = Conversation.start_group(self.alice, 'Trip')
        self.assertFailure(
            self.patch_settings(
                group, {'image': make_image_upload('group.png')}, multipart=True
            )
        )
        group.refresh_from_db()
        self.assertFalse(group.image)

    def test_a_group_name_cannot_be_blanked(self):
        group = Conversation.start_group(self.alice, 'Trip')
        self.assertFailure(self.patch_settings(group, {'name': ''}))
        group.refresh_from_db()
        self.assertEqual(group.name, 'Trip')

    def test_a_plain_member_cannot_edit_the_group(self):
        group = Conversation.start_group(self.alice, 'Trip')
        group.add_participant(self.bob)

        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.patch_settings(group, {'name': 'Hijacked'}),
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_stranger_cannot_edit_the_group(self):
        group = Conversation.start_group(self.alice, 'Trip')

        # 404, not 403: the queryset is the access control, so a non-member
        # cannot even learn that this conversation exists.
        self.client.force_authenticate(self.carol)
        self.assertFailure(
            self.patch_settings(group, {'name': 'Hijacked'}),
            status.HTTP_404_NOT_FOUND,
        )

    def test_a_direct_conversation_has_no_settings_to_edit(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.assertFailure(self.patch_settings(conversation, {'name': 'Nope'}))

    def test_editing_a_group_does_not_bump_it_up_the_conversation_list(self):
        # updated_at is the list's last-activity sort key. Fixing a typo is
        # not activity, so it must not jump the group over a live thread.
        group = Conversation.start_group(self.alice, 'Trip')
        before = Conversation.objects.get(pk=group.pk).updated_at

        self.assertSuccess(self.patch_settings(group, {'description': 'Booked.'}))

        self.assertEqual(Conversation.objects.get(pk=group.pk).updated_at, before)

    # -- membership --------------------------------------------------------

    def make_group(self, admin, name='Team', members=()):
        conversation = Conversation.start_group(admin, name)
        for user in members:
            conversation.add_participant(user)
        return conversation

    def test_admin_can_add_a_member_by_username(self):
        group = self.make_group(self.alice)
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-members', args=[group.id]),
                {'members': ['bob']},
                format='json',
            )
        )
        usernames = {
            p['user']['username'] for p in members_of(self.client, group.id)
        }
        self.assertEqual(usernames, {'alice', 'bob'})

    def test_admin_can_add_a_member_by_phone_number(self):
        group = self.make_group(self.alice)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-members', args=[group.id]),
                {'members': ['+639170000002']},
                format='json',
            )
        )
        self.assertTrue(group.has_participant(self.bob))

    def test_adding_someone_twice_is_harmless(self):
        group = self.make_group(self.alice, members=[self.bob])
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-members', args=[group.id]),
                {'members': ['bob']},
                format='json',
            )
        )
        self.assertEqual(group.participants.filter(user=self.bob).count(), 1)

    def test_a_plain_member_cannot_add_people(self):
        group = self.make_group(self.alice, members=[self.bob])
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-members', args=[group.id]),
                {'members': ['carol']},
                format='json',
            ),
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_stranger_cannot_add_people(self):
        group = self.make_group(self.alice)
        self.client.force_authenticate(self.carol)
        # 404 rather than 403: a non-member is not told the group exists.
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-members', args=[group.id]),
                {'members': ['bob']},
                format='json',
            ),
            status.HTTP_404_NOT_FOUND,
        )

    def test_members_cannot_be_added_to_a_direct_conversation(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-members', args=[conversation.id]),
                {'members': ['carol']},
                format='json',
            )
        )

    # -- access control ----------------------------------------------------

    def test_a_stranger_cannot_read_the_conversation(self):
        group = self.make_group(self.alice, members=[self.bob])
        self.client.force_authenticate(self.carol)
        self.assertFailure(
            self.client.get(reverse('chat:conversation-detail', args=[group.id])),
            status.HTTP_404_NOT_FOUND,
        )

    def test_a_stranger_cannot_read_the_messages(self):
        group = self.make_group(self.alice, members=[self.bob])
        create_message(group, self.alice, text='private')
        self.client.force_authenticate(self.carol)
        self.assertFailure(
            self.client.get(reverse('chat:conversation-messages', args=[group.id])),
            status.HTTP_404_NOT_FOUND,
        )

    def test_a_stranger_cannot_post_a_message(self):
        group = self.make_group(self.alice, members=[self.bob])
        self.client.force_authenticate(self.carol)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-messages', args=[group.id]),
                {'text': 'let me in'},
            ),
            status.HTTP_404_NOT_FOUND,
        )

    def test_the_list_only_shows_my_conversations(self):
        mine = self.make_group(self.alice, name='Mine')
        self.make_group(self.bob, name='Theirs')

        data = self.assertSuccess(self.client.get(reverse('chat:conversation-list')))
        ids = [row['id'] for row in data['results']]
        self.assertEqual(ids, [mine.id])

    # -- messages ----------------------------------------------------------

    def send(self, conversation, payload, fmt='multipart'):
        return self.client.post(
            reverse('chat:conversation-messages', args=[conversation.id]),
            payload,
            format=fmt,
        )

    def test_send_a_text_message(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        data = self.assertSuccess(
            self.send(conversation, {'text': 'hello'}), status.HTTP_201_CREATED
        )
        self.assertEqual(data['text'], 'hello')
        self.assertEqual(data['sender']['username'], 'alice')

    def test_send_emoji_and_links(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        body = 'sure 😄👍 see https://example.com/x?a=1'
        data = self.assertSuccess(
            self.send(conversation, {'text': body}), status.HTTP_201_CREATED
        )
        # Emoji are ordinary characters and links are ordinary text; both
        # must survive the round trip untouched.
        self.assertEqual(data['text'], body)

    def test_send_a_photo(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        data = self.assertSuccess(
            self.send(conversation, {'attachments': make_image_upload('holiday.png')}),
            status.HTTP_201_CREATED,
        )
        attachment = data['attachments'][0]
        self.assertEqual(attachment['kind'], Attachment.Kind.IMAGE)
        self.assertEqual(attachment['original_name'], 'holiday.png')
        self.assertEqual((attachment['width'], attachment['height']), (12, 12))

    def test_send_a_file(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        upload = SimpleUploadedFile('notes.pdf', b'%PDF-1.4 not really', 'application/pdf')
        data = self.assertSuccess(
            self.send(conversation, {'attachments': upload}), status.HTTP_201_CREATED
        )
        attachment = data['attachments'][0]
        # Undecodable as an image, so it is filed as a plain attachment even
        # though nothing but the extension said so.
        self.assertEqual(attachment['kind'], Attachment.Kind.FILE)
        self.assertIsNone(attachment['width'])

    def test_send_several_photos_at_once(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        data = self.assertSuccess(
            self.send(
                conversation,
                {'attachments': [make_image_upload('a.png'), make_image_upload('b.png')]},
            ),
            status.HTTP_201_CREATED,
        )
        self.assertEqual(len(data['attachments']), 2)

    def test_a_photo_can_carry_a_caption(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        data = self.assertSuccess(
            self.send(
                conversation,
                {'text': 'look 😍', 'attachments': make_image_upload()},
            ),
            status.HTTP_201_CREATED,
        )
        self.assertEqual(data['text'], 'look 😍')
        self.assertEqual(len(data['attachments']), 1)

    def test_an_empty_message_is_rejected(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.assertFailure(self.send(conversation, {'text': '   '}))

    @override_settings(MAX_ATTACHMENTS_PER_MESSAGE=2)
    def test_too_many_attachments_is_rejected(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        body = self.assertFailure(
            self.send(
                conversation,
                {'attachments': [make_image_upload(f'{i}.png') for i in range(3)]},
            )
        )
        self.assertIn('attachments', body['data'])

    @override_settings(MAX_ATTACHMENT_UPLOAD_BYTES=10)
    def test_an_oversized_attachment_is_rejected(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.assertFailure(
            self.send(conversation, {'attachments': make_image_upload('big.png')})
        )

    def test_history_is_newest_first_and_paginated(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        for i in range(3):
            create_message(conversation, self.alice, text=f'message {i}')

        data = self.assertSuccess(
            self.client.get(reverse('chat:conversation-messages', args=[conversation.id]))
        )
        self.assertEqual(data['count'], 3)
        self.assertEqual(data['results'][0]['text'], 'message 2')

    def test_the_conversation_list_carries_the_last_message(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        create_message(conversation, self.bob, text='first')
        create_message(conversation, self.bob, text='latest')

        data = self.assertSuccess(self.client.get(reverse('chat:conversation-list')))
        self.assertEqual(data['results'][0]['last_message']['text'], 'latest')

    def test_a_direct_conversation_is_titled_after_the_other_person(self):
        self.bob.profile.display_name = 'Bobby'
        self.bob.profile.save()
        conversation, _ = Conversation.start_direct(self.alice, self.bob)

        data = self.assertSuccess(
            self.client.get(reverse('chat:conversation-detail', args=[conversation.id]))
        )
        self.assertEqual(data['title'], 'Bobby')

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)


@override_settings(ALLOWED_HOSTS=['testserver'])
class ChatWebsocketTests(TransactionTestCase):
    """The realtime path.

    TransactionTestCase rather than TestCase: the consumer reads the database
    from another thread, and `create_message` broadcasts from
    `transaction.on_commit`, so the writes have to genuinely commit.
    """

    # An Origin header is required - the validator refuses a handshake that has
    # none, whatever the allowed list says.
    #
    # This must be an origin from CORS_ALLOWED_ORIGINS, not ALLOWED_HOSTS.
    # `testserver` passed only because the test runner appends it to
    # ALLOWED_HOSTS; against CORS_ALLOWED_ORIGINS it is refused at the origin
    # gate, which would fail the connect test AND make the two rejection tests
    # pass for the wrong reason - never reaching the token check they exist to
    # exercise.
    HEADERS = [(b'origin', b'http://localhost:3000')]

    def setUp(self):
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.carol = make_user('carol', '+639170000003')
        self.conversation, _ = Conversation.start_direct(self.alice, self.bob)
        # Roles only exist on a group, and a notification needs somebody with
        # the standing to cause one.
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)

        # Minted here, in synchronous setUp: RefreshToken.for_user() writes
        # to the database (last_login, outstanding token), which an async
        # test method is not allowed to do directly.
        self.tokens = {
            user.username: str(RefreshToken.for_user(user).access_token)
            for user in (self.alice, self.bob, self.carol)
        }

    def connect_as(self, user):
        from loopd.asgi import application

        return WebsocketCommunicator(
            application,
            f'/ws/chat/?token={self.tokens[user.username]}',
            headers=self.HEADERS,
        )

    async def test_a_socket_marks_them_online_and_closing_it_marks_them_off(self):
        from user import presence
        from user.models import Profile

        await database_sync_to_async(cache.clear)()

        communicator = self.connect_as(self.bob)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        online = await database_sync_to_async(
            lambda: presence.is_online(Profile.objects.get(user=self.bob))
        )()
        self.assertTrue(online)

        await communicator.disconnect()

        still_online = await database_sync_to_async(
            lambda: presence.is_online(Profile.objects.get(user=self.bob))
        )()
        self.assertFalse(still_online)

    async def test_the_other_members_socket_hears_them_arrive(self):
        await database_sync_to_async(cache.clear)()

        watching = self.connect_as(self.alice)
        await watching.connect()

        arriving = self.connect_as(self.bob)
        await arriving.connect()

        frame = await watching.receive_json_from(timeout=3)
        self.assertEqual(frame['type'], 'presence')
        self.assertEqual(frame['user_id'], self.bob.pk)
        self.assertTrue(frame['online'])

        await arriving.disconnect()
        await watching.disconnect()

    async def test_a_notification_reaches_the_socket(self):
        """The half that is easy to leave out and hard to notice.

        Channels routes a group event by turning its `type` into a method
        name, so a frame with no matching method on the consumer is simply
        dropped. The notification row is written either way - which is exactly
        why this looked like it worked until somebody watched for it live
        rather than reloading the page.
        """
        communicator = self.connect_as(self.bob)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await database_sync_to_async(membership.set_role)(
            await database_sync_to_async(Participant.objects.get)(
                conversation=self.group, user=self.bob
            ),
            Participant.Role.ADMIN,
            self.alice,
        )

        frame = await communicator.receive_json_from(timeout=3)
        self.assertEqual(frame['type'], 'notification.new')
        self.assertEqual(frame['notification']['type'], 'role.granted')

        await communicator.disconnect()

    async def test_an_edited_message_reaches_the_socket(self):
        """The same dropped-frame trap, for `message.updated`.

        A reaction, an edit and a delete all change a message that is already
        in everyone's thread, and all three ride this one event. Without
        `message_updated` on the consumer, Channels drops every frame in
        silence - the row is written, so it presents as "works after a refresh,
        never live", which is exactly how the notification bug hid.
        """
        communicator = self.connect_as(self.bob)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        message = await database_sync_to_async(create_message)(
            self.conversation, self.alice, text='dinner at 6'
        )
        # The send itself, which this socket is subscribed to.
        first = await communicator.receive_json_from(timeout=3)
        self.assertEqual(first['type'], 'message.new')

        @database_sync_to_async
        def edit():
            message.text = 'dinner at 7'
            message.edited_at = timezone.now()
            message.save(update_fields=['text', 'edited_at'])
            broadcast_message_updated(message)

        await edit()

        frame = await communicator.receive_json_from(timeout=3)
        self.assertEqual(frame['type'], 'message.updated')
        self.assertEqual(frame['message']['id'], message.pk)
        self.assertEqual(frame['message']['text'], 'dinner at 7')
        self.assertIsNotNone(frame['message']['edited_at'])

        await communicator.disconnect()

    async def test_a_socket_without_a_token_is_refused(self):
        from loopd.asgi import application

        communicator = WebsocketCommunicator(
            application, '/ws/chat/', headers=self.HEADERS
        )
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_a_socket_with_a_junk_token_is_refused(self):
        from loopd.asgi import application

        communicator = WebsocketCommunicator(
            application, '/ws/chat/?token=not-a-jwt', headers=self.HEADERS
        )
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_a_valid_token_connects(self):
        communicator = self.connect_as(self.alice)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

    async def test_a_refused_send_does_not_drop_the_socket(self):
        # A member posting into a broadcast group used to raise
        # PostingNotAllowed out of the consumer, past Channels (which catches
        # only StopConsumer), and Daphne tore the connection down. The user saw
        # a dropped socket instead of "only admins can post", reconnected, and
        # lost everything broadcast in the gap. Any handler bug did the same.
        group = await database_sync_to_async(Conversation.start_group)(
            self.alice, name='Announcements'
        )
        await database_sync_to_async(group.add_participant)(self.bob)
        group.mode = Conversation.Mode.BROADCAST
        await database_sync_to_async(group.save)(update_fields=['mode'])

        communicator = self.connect_as(self.bob)
        await communicator.connect()

        await communicator.send_json_to(
            {'type': 'message.send', 'conversation_id': group.pk, 'text': 'hi'}
        )

        reply = await communicator.receive_json_from()
        self.assertEqual(reply['type'], 'error')
        # Still up, and still usable - not merely "not crashed".
        await communicator.send_json_to({'type': 'nonsense'})
        follow_up = await communicator.receive_json_from()
        self.assertEqual(follow_up['type'], 'error')
        await communicator.disconnect()

    async def test_a_socket_from_an_unlisted_origin_is_refused(self):
        # The whole point of the origin validator, and nothing else asserts it:
        # without one, any website a logged-in user visits could open a socket
        # as them and read their conversations. A VALID token is used
        # deliberately - the refusal must come from the origin gate, so this
        # fails if the validator is ever removed or pointed at a list that
        # happens to contain everything.
        from loopd.asgi import application

        communicator = WebsocketCommunicator(
            application,
            f'/ws/chat/?token={self.tokens[self.alice.username]}',
            headers=[(b'origin', b'http://evil.example.com')],
        )
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_a_socket_with_no_origin_at_all_is_refused(self):
        # A handshake with no Origin header is refused unless the allowed list
        # is '*'. Worth pinning: a non-browser client that sends no Origin -
        # a native mobile app, say - cannot connect, and that is a deliberate
        # property of the validator rather than an accident.
        from loopd.asgi import application

        communicator = WebsocketCommunicator(
            application,
            f'/ws/chat/?token={self.tokens[self.alice.username]}',
            headers=[],
        )
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_a_member_is_pushed_messages_as_they_are_sent(self):
        communicator = self.connect_as(self.bob)
        await communicator.connect()

        await database_sync_to_async(create_message)(
            self.conversation, self.alice, text='ping 👋'
        )

        event = await communicator.receive_json_from(timeout=5)
        self.assertEqual(event['type'], 'message.new')
        self.assertEqual(event['message']['text'], 'ping 👋')
        self.assertEqual(event['message']['sender']['username'], 'alice')

        await communicator.disconnect()

    async def test_a_stranger_is_pushed_nothing(self):
        communicator = self.connect_as(self.carol)
        await communicator.connect()

        await database_sync_to_async(create_message)(
            self.conversation, self.alice, text='not for carol'
        )

        self.assertTrue(await communicator.receive_nothing(timeout=1))
        await communicator.disconnect()

    async def test_sending_over_the_socket_stores_and_broadcasts(self):
        alice_socket = self.connect_as(self.alice)
        bob_socket = self.connect_as(self.bob)
        await alice_socket.connect()
        await bob_socket.connect()

        await alice_socket.send_json_to(
            {
                'type': 'message.send',
                'conversation_id': self.conversation.id,
                'text': 'over the wire',
            }
        )

        event = await bob_socket.receive_json_from(timeout=5)
        self.assertEqual(event['message']['text'], 'over the wire')

        stored = await database_sync_to_async(
            lambda: Message.objects.filter(text='over the wire').count()
        )()
        self.assertEqual(stored, 1)

        await alice_socket.disconnect()
        await bob_socket.disconnect()

    async def test_the_socket_refuses_a_conversation_you_are_not_in(self):
        communicator = self.connect_as(self.carol)
        await communicator.connect()

        await communicator.send_json_to(
            {
                'type': 'message.send',
                'conversation_id': self.conversation.id,
                'text': 'let me in',
            }
        )

        event = await communicator.receive_json_from(timeout=5)
        self.assertEqual(event['type'], 'error')
        stored = await database_sync_to_async(Message.objects.count)()
        self.assertEqual(stored, 0)

        await communicator.disconnect()


class PhoneOnlyParticipantTests(EnvelopeAssertions, APITestCase):
    """Members who have a phone number but no LoopD account.

    Nothing in this class sends anything - the schema has to support these
    people well before the SMS machinery that talks to them exists. What is
    under test is that they can be represented at all, that the database
    refuses the malformed shapes, and above all that their presence does not
    widen anybody's access to the conversation.
    """

    def setUp(self):
        self.alice = make_user('alice', phone='+639170000001')
        self.bob = make_user('bob', phone='+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.client.force_authenticate(self.alice)

    def add_mom(self, number='+639171111111', name='Mom'):
        return self.group.add_phone_participant(
            number, display_name=name, added_by=self.alice
        )

    # --- the shape itself ---------------------------------------------------

    def test_a_member_can_be_a_phone_number_with_no_account(self):
        mom = self.add_mom()
        self.assertIsNone(mom.user_id)
        self.assertTrue(mom.is_phone_only)
        self.assertEqual(mom.display_label, 'Mom')
        self.assertTrue(self.group.has_phone_participant('+639171111111'))

    def test_adding_the_same_number_twice_is_harmless(self):
        first = self.add_mom()
        second = self.add_mom()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.group.participants.count(), 2)

    def test_an_account_member_still_labels_from_their_own_profile(self):
        # display_name is whatever whoever added them typed once; a real
        # profile is the name they keep current, so it has to win.
        self.group.add_participant(self.bob)
        participant = self.group.participants.get(user=self.bob)
        self.assertEqual(participant.display_label, self.bob.profile.display_label)

    # --- what the database refuses -----------------------------------------

    def test_a_membership_must_identify_somebody(self):
        from django.db.utils import IntegrityError

        with self.assertRaises(IntegrityError):
            Participant.objects.create(conversation=self.group)

    def test_a_phone_only_member_must_have_a_name(self):
        # Otherwise the group renders a bare phone number to everybody in it,
        # which the per-group-number design exists to avoid.
        from django.db.utils import IntegrityError

        with self.assertRaises(IntegrityError):
            Participant.objects.create(
                conversation=self.group, phone_number='+639172222222'
            )

    def test_the_same_number_cannot_join_one_group_twice(self):
        from django.db.utils import IntegrityError

        self.add_mom()
        with self.assertRaises(IntegrityError):
            Participant.objects.create(
                conversation=self.group,
                phone_number='+639171111111',
                display_name='Mum',
            )

    def test_the_same_number_may_be_in_two_different_groups(self):
        other = Conversation.start_group(self.alice, name='Work')
        self.add_mom()
        other.add_phone_participant('+639171111111', display_name='Mom')
        self.assertEqual(
            Participant.objects.filter(phone_number='+639171111111').count(), 2
        )

    # --- access control is unchanged ---------------------------------------

    def test_a_phone_only_member_grants_nobody_access(self):
        # The access-control queryset is `participants__user=request.user`.
        # A row with a NULL user must never satisfy it for anyone.
        self.add_mom()
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.get(reverse('chat:conversation-detail', args=[self.group.id])),
            status.HTTP_404_NOT_FOUND,
        )

    def test_a_phone_only_member_does_not_appear_in_anyones_conversation_list(self):
        self.add_mom()
        self.client.force_authenticate(self.bob)
        data = self.assertSuccess(self.client.get(reverse('chat:conversation-list')))
        self.assertEqual(data['results'], [])

    def test_the_group_still_lists_normally_for_a_real_member(self):
        self.add_mom()
        # The group itself is readable...
        self.assertSuccess(
            self.client.get(reverse('chat:conversation-detail', args=[self.group.id]))
        )
        # ...and its members come from the member endpoint.
        rows = members_of(self.client, self.group.id)

        labels = {p['display_label'] for p in rows}
        self.assertEqual(labels, {self.alice.profile.display_label, 'Mom'})

        mom = next(p for p in rows if p['is_phone_only'])
        self.assertIsNone(mom['user'])
        # Everybody receives texts by default. A member verifies their number
        # when they sign up precisely so their groups can reach them on it, and
        # having the app installed does not change that - the app and SMS are
        # two ways into one conversation.
        self.assertTrue(mom['sms_enabled'])
        alice_row = next(p for p in rows if not p['is_phone_only'])
        self.assertTrue(alice_row['sms_enabled'])

    # --- registering later claims the row rather than duplicating it -------

    def test_registering_claims_the_phone_row_instead_of_adding_a_second(self):
        mom = self.add_mom(number='+639171111111')
        # She registers and verifies that number.
        real_mom = make_user('mom', phone='+639171111111')

        claimed = self.group.add_participant(real_mom)

        self.assertEqual(claimed.pk, mom.pk)
        self.assertEqual(claimed.user_id, real_mom.pk)
        self.assertEqual(self.group.participants.count(), 2)
        # Her own profile name now wins over whatever alice typed.
        self.assertEqual(claimed.display_label, real_mom.profile.display_label)

    def test_claiming_keeps_the_messages_she_texted_in(self):
        mom = self.add_mom(number='+639171111111')
        create_message(
            conversation=self.group,
            sender=None,
            text='On my way',
            source=Message.Source.SMS,
            participant=mom,
        )
        real_mom = make_user('mom', phone='+639171111111')
        claimed = self.group.add_participant(real_mom)

        message = Message.objects.get(text='On my way')
        self.assertEqual(message.sender_participant_id, claimed.pk)

    def test_an_unverified_number_never_claims_somebody_elses_row(self):
        # The number is the only evidence of who this row belongs to, so an
        # unproven claim on it must not hand over the membership.
        self.add_mom(number='+639171111111')
        impostor = User.objects.create_user(username='eve', password='x')
        impostor.profile.phone_number = '+639171111111'
        impostor.profile.save()

        self.group.add_participant(impostor)

        self.assertEqual(self.group.participants.count(), 3)
        self.assertTrue(
            self.group.participants.filter(
                user__isnull=True, phone_number='+639171111111'
            ).exists()
        )

    def test_being_in_twice_merges_down_to_one_row(self):
        # Reachable via the admin or the shell: added as an account, and
        # separately as a number that later gets verified.
        bob_row = self.group.add_participant(self.bob)
        phone_row = self.group.add_phone_participant(
            self.bob.profile.phone_number, display_name='Bobby', added_by=self.alice
        )
        create_message(
            conversation=self.group,
            sender=None,
            text='texted in',
            source=Message.Source.SMS,
            participant=phone_row,
        )

        survivor = self.group.add_participant(self.bob)

        self.assertEqual(survivor.pk, bob_row.pk)
        self.assertFalse(Participant.objects.filter(pk=phone_row.pk).exists())
        # The history moved across rather than being orphaned by SET_NULL.
        self.assertEqual(
            Message.objects.get(text='texted in').sender_participant_id, bob_row.pk
        )

    # --- messages from somebody with no account ----------------------------

    def test_a_message_can_be_sent_by_a_phone_only_member(self):
        mom = self.add_mom()
        message = create_message(
            conversation=self.group,
            sender=None,
            text='On my way',
            source=Message.Source.SMS,
            participant=mom,
        )
        self.assertIsNone(message.sender_id)
        self.assertEqual(message.sender_participant_id, mom.pk)
        self.assertEqual(message.source, Message.Source.SMS)
        self.assertEqual(message.sender_label, 'Mom')

    def test_such_a_message_serialises_to_the_same_shape_as_any_other(self):
        # Every client reads sender.profile.display_label. If a sender with no
        # account came back as null, all of them would break on one message.
        mom = self.add_mom()
        create_message(
            conversation=self.group,
            sender=None,
            text='On my way',
            source=Message.Source.SMS,
            participant=mom,
        )
        data = self.assertSuccess(
            self.client.get(reverse('chat:conversation-messages', args=[self.group.id]))
        )
        row = data['results'][0]
        self.assertEqual(row['source'], 'sms')
        self.assertEqual(row['sender']['profile']['display_label'], 'Mom')
        self.assertIsNone(row['sender']['id'])
        self.assertIsNone(row['sender']['username'])

    def test_an_ordinary_message_is_unchanged_and_credits_its_membership(self):
        message = create_message(
            conversation=self.group, sender=self.alice, text='hello'
        )
        self.assertEqual(message.source, Message.Source.APP)
        # Resolved automatically, so no existing caller had to change.
        self.assertIsNotNone(message.sender_participant_id)
        self.assertEqual(message.sender_participant.user_id, self.alice.pk)


class MembershipStateTests(EnvelopeAssertions, APITestCase):
    """Joining, leaving, approval, removal, blocking and muting.

    The rule under test throughout is that `Participant.status` is honoured
    everywhere, not just where it was convenient: a row that is not ACTIVE must
    not read history, appear in a member list, or reach the access-control
    queryset.
    """

    def setUp(self):
        self.alice = make_user('alice', phone='+639170000001')
        self.bob = make_user('bob', phone='+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.client.force_authenticate(self.alice)

    def join_as(self, user, code=None):
        self.client.force_authenticate(user)
        return self.client.post(
            reverse('chat:conversation-join'),
            {'code': code if code is not None else self.group.join_code},
            format='json',
        )

    def set_mode(self, mode):
        self.group.join_mode = mode
        self.group.save(update_fields=['join_mode'])

    # --- join codes and modes ----------------------------------------------

    def test_a_new_group_gets_a_join_code_and_is_invite_only(self):
        self.assertTrue(self.group.join_code)
        self.assertEqual(self.group.join_mode, Conversation.JoinMode.INVITE)

    def test_invite_only_refuses_a_join_even_with_the_right_code(self):
        self.assertFailure(self.join_as(self.bob))
        self.assertFalse(self.group.has_participant(self.bob))

    def test_open_lets_somebody_straight_in(self):
        self.set_mode(Conversation.JoinMode.OPEN)
        self.assertSuccess(self.join_as(self.bob))
        self.assertTrue(self.group.has_participant(self.bob))

    def test_open_group_can_be_joined_without_a_code(self):
        self.set_mode(Conversation.JoinMode.OPEN)
        self.client.force_authenticate(self.bob)

        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-join-open', args=[self.group.id])
            )
        )

        self.assertEqual(data['id'], self.group.id)
        self.assertEqual(
            self.group.participants.get(user=self.bob).status,
            Participant.Status.ACTIVE,
        )

    def test_approval_group_creates_a_pending_join_request_without_a_code(self):
        self.set_mode(Conversation.JoinMode.APPROVAL)
        self.client.force_authenticate(self.bob)

        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-join-open', args=[self.group.id])
            )
        )

        self.assertEqual(data, {'status': Participant.Status.PENDING})
        self.assertEqual(
            self.group.participants.get(user=self.bob).status,
            Participant.Status.PENDING,
        )

        self.client.force_authenticate(self.alice)
        requests = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-requests', args=[self.group.id])
            )
        )
        self.assertEqual(requests[0]['user']['username'], 'bob')

    def test_a_wrong_code_is_refused(self):
        self.set_mode(Conversation.JoinMode.OPEN)
        self.assertFailure(self.join_as(self.bob, code='WRONGX'))
        self.assertFalse(self.group.has_participant(self.bob))

    def test_a_code_is_matched_case_insensitively(self):
        # It gets read off a screen and typed into a text message.
        self.set_mode(Conversation.JoinMode.CODE)
        self.assertSuccess(self.join_as(self.bob, code=self.group.join_code.lower()))
        self.assertTrue(self.group.has_participant(self.bob))

    def test_regenerating_the_code_invalidates_the_old_one(self):
        self.set_mode(Conversation.JoinMode.OPEN)
        old = self.group.join_code
        self.group.regenerate_join_code()
        self.assertFailure(self.join_as(self.bob, code=old))

    def test_joining_twice_does_not_make_two_rows(self):
        self.set_mode(Conversation.JoinMode.OPEN)
        self.assertSuccess(self.join_as(self.bob))
        self.assertSuccess(self.join_as(self.bob))
        self.assertEqual(self.group.participants.filter(user=self.bob).count(), 1)

    # --- approval ----------------------------------------------------------

    def test_approval_mode_makes_a_pending_request_not_a_member(self):
        self.set_mode(Conversation.JoinMode.APPROVAL)
        self.assertSuccess(self.join_as(self.bob))

        row = self.group.participants.get(user=self.bob)
        self.assertEqual(row.status, Participant.Status.PENDING)
        # Pending is not membership: no history, no listing.
        self.assertFalse(self.group.has_participant(self.bob))
        self.assertFailure(
            self.client.get(
                reverse('chat:conversation-messages', args=[self.group.id])
            ),
            status.HTTP_404_NOT_FOUND,
        )

    def test_an_admin_sees_and_approves_a_pending_request(self):
        self.set_mode(Conversation.JoinMode.APPROVAL)
        self.join_as(self.bob)

        self.client.force_authenticate(self.alice)
        url = reverse('chat:conversation-requests', args=[self.group.id])
        waiting = self.assertSuccess(self.client.get(url))
        self.assertEqual([r['display_label'] for r in waiting],
                         [self.bob.profile.display_label])

        row = self.group.participants.get(user=self.bob)
        self.assertSuccess(
            self.client.post(url, {'participant': row.pk, 'decision': 'approve'},
                             format='json')
        )
        self.assertTrue(self.group.has_participant(self.bob))

    def test_declining_leaves_them_out(self):
        self.set_mode(Conversation.JoinMode.APPROVAL)
        self.join_as(self.bob)
        row = self.group.participants.get(user=self.bob)

        self.client.force_authenticate(self.alice)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-requests', args=[self.group.id]),
                {'participant': row.pk, 'decision': 'decline'},
                format='json',
            )
        )
        self.assertFalse(self.group.has_participant(self.bob))

    def test_a_plain_member_cannot_review_requests(self):
        self.group.add_participant(self.bob)
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.get(reverse('chat:conversation-requests', args=[self.group.id])),
            status.HTTP_403_FORBIDDEN,
        )

    # --- leaving, removal, blocking ----------------------------------------

    def test_leaving_ends_access_but_keeps_the_messages(self):
        self.group.add_participant(self.bob)
        create_message(conversation=self.group, sender=self.bob, text='bye')

        self.client.force_authenticate(self.bob)
        self.assertSuccess(
            self.client.post(reverse('chat:conversation-leave', args=[self.group.id]))
        )

        self.assertFalse(self.group.has_participant(self.bob))
        self.assertFailure(
            self.client.get(reverse('chat:conversation-detail', args=[self.group.id])),
            status.HTTP_404_NOT_FOUND,
        )
        self.assertTrue(Message.objects.filter(text='bye').exists())

    def test_rejoining_after_leaving_reuses_the_row(self):
        self.set_mode(Conversation.JoinMode.OPEN)
        self.group.add_participant(self.bob)
        membership.leave(self.group.participants.get(user=self.bob))

        self.assertSuccess(self.join_as(self.bob))
        self.assertEqual(self.group.participants.filter(user=self.bob).count(), 1)
        self.assertTrue(self.group.has_participant(self.bob))

    def test_an_admin_removes_a_member(self):
        self.group.add_participant(self.bob)
        row = self.group.participants.get(user=self.bob)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-remove', args=[self.group.id]),
                {'participant': row.pk},
                format='json',
            )
        )
        self.assertFalse(self.group.has_participant(self.bob))

    def test_a_blocked_member_cannot_get_back_in_by_any_route(self):
        self.set_mode(Conversation.JoinMode.OPEN)
        self.group.add_participant(self.bob)
        row = self.group.participants.get(user=self.bob)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-remove', args=[self.group.id]),
                {'participant': row.pk, 'block': True},
                format='json',
            )
        )

        # Not by joining, even though the group is wide open. The refusal is
        # indistinguishable from a wrong code, on purpose.
        self.assertFailure(self.join_as(self.bob))
        # ...and not by an admin adding them back.
        self.client.force_authenticate(self.alice)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-members', args=[self.group.id]),
                {'members': ['bob']},
                format='json',
            ),
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(self.group.has_participant(self.bob))

    def test_the_last_admin_cannot_be_removed(self):
        # Otherwise the group is left with nobody who can approve, add or edit.
        row = self.group.participants.get(user=self.alice)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-remove', args=[self.group.id]),
                {'participant': row.pk},
                format='json',
            )
        )

    def test_a_plain_member_cannot_remove_anybody(self):
        self.group.add_participant(self.bob)
        alice_row = self.group.participants.get(user=self.alice)
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-remove', args=[self.group.id]),
                {'participant': alice_row.pk},
                format='json',
            )
        )

    # --- muting ------------------------------------------------------------

    def test_muting_keeps_membership_but_records_the_choice(self):
        self.group.add_participant(self.bob)
        self.client.force_authenticate(self.bob)
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-delivery', args=[self.group.id]),
                {'muted': True},
                format='json',
            )
        )
        self.assertTrue(data['muted'])
        # Still a member: mute silences delivery, it does not remove anybody.
        self.assertTrue(self.group.has_participant(self.bob))
        self.assertSuccess(
            self.client.get(reverse('chat:conversation-detail', args=[self.group.id]))
        )

    # --- the member list ---------------------------------------------------

    def test_inactive_rows_never_appear_in_the_member_list(self):
        self.group.add_participant(self.bob)
        carol = make_user('carol')
        self.group.add_participant(carol)
        membership.leave(self.group.participants.get(user=carol))

        labels = {
            p['display_label'] for p in members_of(self.client, self.group.id)
        }
        self.assertEqual(
            labels,
            {self.alice.profile.display_label, self.bob.profile.display_label},
        )


class BlockedMemberCannotReturnTests(EnvelopeAssertions, APITestCase):
    """A block must hold whichever identifier the person comes back with.

    One human has up to two: an account and a phone number. Every one of these
    was a real hole - blocking by one left the other wide open.
    """

    def setUp(self):
        self.alice = make_user('alice', phone='+639170000001')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.join_mode = Conversation.JoinMode.OPEN
        self.group.save(update_fields=['join_mode'])
        self.client.force_authenticate(self.alice)

    def block(self, participant):
        return membership.remove(participant, self.alice, block=True)

    def test_blocked_by_account_cannot_return_by_texting_join(self):
        bob = make_user('bob', phone='+639170000002')
        self.block(self.group.add_participant(bob))

        result = inbound.handle_inbound(self.group, '+639170000002', 'JOIN')

        self.assertEqual(result.action, 'refused_blocked')
        self.assertIsNone(result.reply)
        self.assertFalse(self.group.has_participant(bob))
        self.assertEqual(self.group.participants.count(), 2)

    def test_blocked_by_account_cannot_post_by_text_either(self):
        bob = make_user('bob', phone='+639170000002')
        self.block(self.group.add_participant(bob))

        result = inbound.handle_inbound(self.group, '+639170000002', 'hello?')

        self.assertEqual(result.action, 'ignored_unknown_sender')
        self.assertEqual(Message.objects.count(), 0)

    def test_blocked_by_number_cannot_return_by_registering_an_account(self):
        phone_row = self.group.add_phone_participant(
            '+639170000003', display_name='Mom', added_by=self.alice
        )
        self.block(phone_row)

        # They register and verify the very number that was blocked.
        mom = make_user('mom', phone='+639170000003')
        self.client.force_authenticate(mom)
        refused = self.assertFailure(
            self.client.post(
                reverse('chat:conversation-join'),
                {'code': self.group.join_code},
                format='json',
            )
        )
        self.assertFalse(self.group.has_participant(mom))

        # And the refusal is word-for-word what a wrong code gets. A blocked
        # person who could tell the two apart would know their guessed code is
        # real, which is the whole of what code-harvesting needs.
        wrong = self.assertFailure(
            self.client.post(
                reverse('chat:conversation-join'), {'code': 'ZZZZZZ'}, format='json'
            )
        )
        self.assertEqual(refused, wrong)

    def test_an_admin_cannot_re_add_a_blocked_number_by_username(self):
        # The prefetch cache hid the blocked row from add_participant, so this
        # quietly succeeded and created a fresh active row.
        phone_row = self.group.add_phone_participant(
            '+639170000004', display_name='Mom', added_by=self.alice
        )
        self.block(phone_row)
        mom = make_user('mom', phone='+639170000004')

        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-members', args=[self.group.id]),
                {'members': ['mom']},
                format='json',
            ),
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(self.group.has_participant(mom))

    def test_removing_without_block_never_lifts_an_existing_block(self):
        bob = make_user('bob', phone='+639170000002')
        row = self.block(self.group.add_participant(bob))

        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-remove', args=[self.group.id]),
                {'participant': row.pk, 'block': False},
                format='json',
            )
        )
        row.refresh_from_db()
        self.assertEqual(row.status, Participant.Status.BLOCKED)


class MembershipGuardTests(EnvelopeAssertions, APITestCase):
    """Guards that stop a group being stranded or a thread being lost."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.client.force_authenticate(self.alice)

    def test_the_last_admin_cannot_leave(self):
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)

        self.assertFailure(
            self.client.post(reverse('chat:conversation-leave', args=[group.id]))
        )
        self.assertTrue(group.is_admin(self.alice))

    def test_an_admin_can_leave_once_there_is_another(self):
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob, role=Participant.Role.ADMIN)

        self.assertSuccess(
            self.client.post(reverse('chat:conversation-leave', args=[group.id]))
        )
        self.assertFalse(group.has_participant(self.alice))

    def test_you_cannot_leave_a_one_to_one_conversation(self):
        # There is no way back into one: start_direct finds the existing row by
        # direct_key and never reactivates a membership, so leaving would lock
        # somebody out permanently while the other person kept writing.
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-leave', args=[conversation.id])
            )
        )
        self.assertTrue(conversation.has_participant(self.alice))

    def test_an_invite_only_code_is_indistinguishable_from_a_wrong_one(self):
        # Otherwise the endpoint confirms which codes are real.
        group = Conversation.start_group(self.alice, name='Private')
        self.client.force_authenticate(self.bob)

        real = self.assertFailure(
            self.client.post(
                reverse('chat:conversation-join'),
                {'code': group.join_code},
                format='json',
            )
        )
        wrong = self.assertFailure(
            self.client.post(
                reverse('chat:conversation-join'), {'code': 'ZZZZZZ'}, format='json'
            )
        )
        self.assertEqual(str(real['message']), str(wrong['message']))


class RolesAndJoinSettingsTests(EnvelopeAssertions, APITestCase):
    """Handing over a group, and controlling how people get into it."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.client.force_authenticate(self.alice)

    def role_url(self):
        return reverse('chat:conversation-role', args=[self.group.id])

    def code_url(self):
        return reverse('chat:conversation-join-code', args=[self.group.id])

    def settings_url(self):
        return reverse('chat:conversation-settings', args=[self.group.id])

    def row(self, user):
        return Participant.objects.get(conversation=self.group, user=user)

    # --- handover -----------------------------------------------------------

    def test_the_only_admin_can_hand_over_and_then_leave(self):
        # The whole point of the role endpoint: leave and remove both refuse to
        # take the last admin, so without this the founder is stuck forever.
        self.assertFailure(
            self.client.post(reverse('chat:conversation-leave', args=[self.group.id]))
        )

        self.assertSuccess(
            self.client.post(
                self.role_url(),
                {'participant': self.row(self.bob).pk, 'role': 'admin'},
                format='json',
            )
        )
        self.assertSuccess(
            self.client.post(reverse('chat:conversation-leave', args=[self.group.id]))
        )
        self.assertFalse(self.group.has_participant(self.alice))
        self.assertTrue(self.group.is_admin(self.bob))

    def test_the_last_admin_cannot_demote_themselves(self):
        self.assertFailure(
            self.client.post(
                self.role_url(),
                {'participant': self.row(self.alice).pk, 'role': 'member'},
                format='json',
            )
        )
        self.assertTrue(self.group.is_admin(self.alice))

    def test_a_plain_member_cannot_promote_anybody(self):
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.post(
                self.role_url(),
                {'participant': self.row(self.bob).pk, 'role': 'admin'},
                format='json',
            )
        )
        self.assertFalse(self.group.is_admin(self.bob))

    def test_somebody_with_no_account_cannot_be_made_admin(self):
        # Every admin action is an authenticated REST call, so the role would be
        # unusable - while still counting towards the last-admin guard.
        mom = self.group.add_phone_participant(
            '+639171111111', display_name='Mom', added_by=self.alice
        )
        self.assertFailure(
            self.client.post(
                self.role_url(), {'participant': mom.pk, 'role': 'admin'}, format='json'
            )
        )

    def test_a_removed_member_cannot_be_promoted(self):
        membership.remove(self.row(self.bob), self.alice)
        self.assertFailure(
            self.client.post(
                self.role_url(),
                {'participant': self.row(self.bob).pk, 'role': 'admin'},
                format='json',
            )
        )

    # --- join settings ------------------------------------------------------

    def test_an_admin_changes_how_people_join(self):
        data = self.assertSuccess(
            self.client.patch(
                self.settings_url(), {'settings': {'join_mode': 'approval'}}, format='json'
            )
        )
        self.assertEqual(data['settings']['join_mode'], 'approval')

    def test_switching_to_code_mode_guarantees_there_is_a_code(self):
        # A group can predate join codes, or have had one revoked; the setting
        # must not claim a mode the group cannot actually serve.
        Conversation.objects.filter(pk=self.group.pk).update(join_code=None)
        self.assertSuccess(
            self.client.patch(self.settings_url(), {'settings': {'join_mode': 'code'}}, format='json')
        )
        self.group.refresh_from_db()
        self.assertTrue(self.group.join_code)

    def test_editing_settings_does_not_bump_the_group_up_the_list(self):
        before = Conversation.objects.get(pk=self.group.pk).updated_at
        self.assertSuccess(
            self.client.patch(self.settings_url(), {'settings': {'join_mode': 'open'}}, format='json')
        )
        self.assertEqual(Conversation.objects.get(pk=self.group.pk).updated_at, before)

    def test_the_conversation_payload_carries_every_group_setting(self):
        # A settings screen has to be renderable from one fetch. Without this a
        # client can PATCH all seven and read back only two, so it cannot show
        # the user what the group is actually set to.
        response = self.client.patch(
            self.settings_url(),
            {
                'settings': {
                    'add_member_permission': 'admins',
                    'edit_group_permission': 'admins',
                    'sms_delivery_enabled': False,
                    'sms_replies_enabled': False,
                    'voice_messages_enabled': False,
                }
            },
            format='json',
        )
        self.assertSuccess(response)

        data = response.json()['data']
        settings = data['settings']
        self.assertEqual(settings['add_member_permission'], 'admins')
        self.assertEqual(settings['edit_group_permission'], 'admins')
        self.assertFalse(settings['sms_delivery_enabled'])
        self.assertFalse(settings['sms_replies_enabled'])
        self.assertFalse(settings['voice_messages_enabled'])
        # The two that were already exposed, now in here with the rest.
        self.assertIn('mode', settings)
        self.assertIn('join_mode', settings)

        # One object, not two places to look: they are no longer at the top
        # level, so a client has exactly one shape to read.
        self.assertNotIn('mode', data)
        self.assertNotIn('join_mode', data)

        # And NOT the join code: it lets a stranger in, so it stays on its own
        # admin-only endpoint rather than riding along on every fetch.
        self.assertNotIn('join_code', settings)
        self.assertNotIn('join_code', data)

    def test_settings_replies_without_the_member_list(self):
        # The endpoint changes the group, not who is in it. Returning every
        # participant made the reply grow with the size of the group and
        # re-send what the client already had.
        data = self.assertSuccess(
            self.client.patch(
                self.settings_url(), {'name': 'Renamed'}, format='json'
            )
        )
        self.assertNotIn('participants', data)
        # But it is still the conversation - the rest of the shape is intact.
        self.assertEqual(data['name'], 'Renamed')
        self.assertIn('settings', data)
        self.assertIn('sms_number', data)

    # --- the member list ----------------------------------------------------

    def participants_url(self):
        return reverse('chat:conversation-participants', args=[self.group.id])

    def test_the_member_list_is_paginated_ten_at_a_time(self):
        # 10 rather than the project default of 20: this renders as rows in a
        # sheet, where the first page wants to be one screen.
        for i in range(15):
            self.group.add_participant(make_user(f'member{i}'))

        data = self.assertSuccess(self.client.get(self.participants_url()))

        # alice + bob from setUp, plus 15 = 17 active members.
        self.assertEqual(data['count'], 17)
        self.assertEqual(len(data['results']), 10)
        self.assertIsNotNone(data['next'])
        self.assertIsNone(data['previous'])

        page_two = self.assertSuccess(
            self.client.get(self.participants_url(), {'page': 2})
        )
        self.assertEqual(len(page_two['results']), 7)

        # No row appears on both pages: joined_at alone is not unique, so the
        # sort has to fall back to pk or pagination silently drops people.
        first = {row['id'] for row in data['results']}
        second = {row['id'] for row in page_two['results']}
        self.assertEqual(len(first | second), 17)
        self.assertFalse(first & second)

    def test_a_client_may_ask_for_a_bigger_page(self):
        for i in range(15):
            self.group.add_participant(make_user(f'member{i}'))

        data = self.assertSuccess(
            self.client.get(self.participants_url(), {'page_size': 50})
        )
        self.assertEqual(len(data['results']), 17)

    def test_the_member_list_shows_only_active_members(self):
        gone = make_user('gone')
        self.group.add_participant(gone)
        membership.remove(
            Participant.objects.get(conversation=self.group, user=gone),
            by=self.alice,
        )

        data = self.assertSuccess(self.client.get(self.participants_url()))

        self.assertEqual(data['count'], 2)
        self.assertNotIn(
            'gone', [row['user']['username'] for row in data['results']]
        )

    def test_a_non_member_cannot_read_the_member_list(self):
        # 404, not 403: access control is the queryset, so an outsider cannot
        # even learn the conversation exists.
        outsider = make_user('outsider')
        self.client.force_authenticate(outsider)

        response = self.client.get(self.participants_url())

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # --- the join code ------------------------------------------------------

    def test_an_admin_reads_and_reissues_the_code(self):
        first = self.assertSuccess(self.client.get(self.code_url()))['join_code']
        self.assertEqual(first, self.group.join_code)

        second = self.assertSuccess(self.client.post(self.code_url()))['join_code']
        self.assertNotEqual(first, second)

    def test_reissuing_invalidates_the_old_code(self):
        self.group.join_mode = Conversation.JoinMode.OPEN
        self.group.save(update_fields=['join_mode'])
        old = self.group.join_code
        self.client.post(self.code_url())

        carol = make_user('carol')
        self.client.force_authenticate(carol)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-join'), {'code': old}, format='json'
            )
        )

    def test_a_plain_member_cannot_see_the_join_code(self):
        # It is the thing that lets a stranger in, so it stays with the people
        # who administer the group rather than on every conversation fetch.
        self.client.force_authenticate(self.bob)
        self.assertFailure(self.client.get(self.code_url()), status.HTTP_403_FORBIDDEN)

    def test_the_join_code_is_not_in_the_conversation_payload(self):
        data = self.assertSuccess(
            self.client.get(reverse('chat:conversation-detail', args=[self.group.id]))
        )
        self.assertNotIn('join_code', data)
        # Nor inside the settings object, which is where the rest of them live.
        self.assertNotIn('join_code', data['settings'])
        self.assertIn('join_mode', data['settings'])


class ChannelLayerCheckTests(SimpleTestCase):
    """The deploy gate for realtime.

    An in-memory channel layer across two tasks drops messages silently -
    `send_to_group` guards only against the layer being None, and
    InMemoryChannelLayer is not None, so every broadcast reports success. It
    presents as "realtime works, then stops, and refreshing fixes it".
    """

    IN_MEMORY = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
    REDIS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': ['redis://localhost:6379/0']},
        }
    }
    POSTGRES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'x'}}
    SQLITE = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}

    def run_check(self):
        from chat.checks import check_channel_layer

        return [problem.id for problem in check_channel_layer(None)]

    def test_a_deployment_without_redis_is_refused(self):
        with override_settings(
            DEBUG=False, CHANNEL_LAYERS=self.IN_MEMORY, DATABASES=self.POSTGRES
        ):
            self.assertIn('chat.E001', self.run_check())

    def test_a_deployment_with_redis_is_fine(self):
        with override_settings(
            DEBUG=False, CHANNEL_LAYERS=self.REDIS, DATABASES=self.POSTGRES
        ):
            self.assertEqual(self.run_check(), [])

    def test_ci_is_not_a_deployment(self):
        # CI runs `check --deploy` with DEBUG off and neither POSTGRES_DB nor
        # REDIS_URL, on purpose. Failing there would break every build for a
        # condition that is only ever true of a real deployment.
        with override_settings(
            DEBUG=False, CHANNEL_LAYERS=self.IN_MEMORY, DATABASES=self.SQLITE
        ):
            self.assertEqual(self.run_check(), [])

    def test_a_laptop_is_not_a_deployment(self):
        with override_settings(
            DEBUG=True, CHANNEL_LAYERS=self.IN_MEMORY, DATABASES=self.POSTGRES
        ):
            self.assertEqual(self.run_check(), [])


class ConversationAdminFieldsTests(EnvelopeAssertions, APITestCase):
    """Who started this group, who runs it, and whether you do."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)

    def detail(self, user):
        self.client.force_authenticate(user)
        return self.assertSuccess(
            self.client.get(reverse('chat:conversation-detail', args=[self.group.id]))
        )

    def test_created_by_is_a_person_not_an_id(self):
        data = self.detail(self.alice)

        self.assertEqual(
            data['created_by'], {'username': 'alice', 'avatar': None}
        )

    def test_the_creator_is_an_admin(self):
        self.assertTrue(self.detail(self.alice)['is_admin'])

    def test_an_ordinary_member_is_not(self):
        self.assertFalse(self.detail(self.bob)['is_admin'])

    def test_a_promoted_member_is(self):
        # The other half of the rule: not the creator, so it has to come from
        # the membership row.
        membership.set_role(
            Participant.objects.get(conversation=self.group, user=self.bob),
            Participant.Role.ADMIN,
            by=self.alice,
        )

        self.assertTrue(self.detail(self.bob)['is_admin'])

    def test_admin_details_names_the_admin_to_a_member(self):
        # Bob does not administer the group, so this tells him who does.
        self.assertEqual(
            self.detail(self.bob)['admin_details'],
            {'username': 'alice', 'avatar': None},
        )

    def test_admin_details_prefers_the_creator_over_a_later_admin(self):
        # Two admins, one object to return: the answer must be stable rather
        # than whichever row the database hands back first.
        membership.set_role(
            Participant.objects.get(conversation=self.group, user=self.bob),
            Participant.Role.ADMIN,
            by=self.alice,
        )
        carol = make_user('carol')
        self.group.add_participant(carol)

        self.assertEqual(
            self.detail(carol)['admin_details']['username'], 'alice'
        )

    def test_a_direct_conversation_has_no_admin(self):
        # Nobody administers a one-to-one, so claiming somebody does would put
        # admin controls on a screen that has none.
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.client.force_authenticate(self.alice)

        data = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-detail', args=[conversation.id])
            )
        )

        self.assertFalse(data['is_admin'])
        self.assertIsNone(data['admin_details'])
        self.assertEqual(data['created_by']['username'], 'alice')

    def test_the_new_fields_do_not_cost_a_query_per_conversation(self):
        # created_by and its avatar are on every row of the list. Without the
        # select_related this is two extra queries per conversation, which only
        # shows up once somebody has a lot of them.
        for i in range(6):
            Conversation.start_group(self.alice, name=f'Group {i}')
        self.client.force_authenticate(self.alice)
        url = reverse('chat:conversation-list')

        with CaptureQueriesContext(connection) as first:
            self.client.get(url)

        for i in range(6, 12):
            Conversation.start_group(self.alice, name=f'Group {i}')
        with CaptureQueriesContext(connection) as second:
            self.client.get(url)

        self.assertEqual(len(second), len(first))


class SeamlessReconnectTests(EnvelopeAssertions, APITestCase):
    """What a client needs for a dropped socket to be invisible.

    A socket always drops eventually - a sleeping laptop, a mobile handoff, a
    deploy cycling tasks. These are the three things that decide whether that
    costs the user anything.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.client.force_authenticate(self.alice)

    def messages_url(self):
        return reverse('chat:conversation-messages', args=[self.group.id])

    # --- resuming -----------------------------------------------------------

    def test_a_client_can_fetch_exactly_what_it_missed(self):
        seen = create_message(self.group, self.bob, text='before you left')
        missed = [
            create_message(self.group, self.bob, text=f'missed {i}')
            for i in range(3)
        ]

        data = self.assertSuccess(
            self.client.get(self.messages_url(), {'after_id': seen.pk})
        )

        self.assertEqual(
            [row['id'] for row in data['results']], [m.pk for m in missed]
        )

    def test_resuming_from_the_newest_message_returns_nothing(self):
        newest = create_message(self.group, self.bob, text='up to date')

        data = self.assertSuccess(
            self.client.get(self.messages_url(), {'after_id': newest.pk})
        )

        self.assertEqual(data['results'], [])

    def test_a_junk_cursor_is_refused_rather_than_ignored(self):
        # Silently ignoring it would hand back the whole history as though the
        # client were resuming from nothing, and it would never know.
        self.assertFailure(
            self.client.get(self.messages_url(), {'after_id': 'yesterday'})
        )

    # --- retrying -----------------------------------------------------------

    def test_a_retried_send_does_not_post_twice(self):
        first = create_message(
            self.group, self.alice, text='did that land?', client_id='abc-123'
        )
        again = create_message(
            self.group, self.alice, text='did that land?', client_id='abc-123'
        )

        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Message.objects.filter(conversation=self.group).count(), 1)

    def test_two_different_keys_are_two_messages(self):
        create_message(self.group, self.alice, text='one', client_id='a')
        create_message(self.group, self.alice, text='two', client_id='b')

        self.assertEqual(Message.objects.filter(conversation=self.group).count(), 2)

    def test_an_unkeyed_send_is_never_deduplicated(self):
        # Inbound SMS and older clients send no key. Two identical texts are two
        # messages, and collapsing them would silently drop somebody's message.
        create_message(self.group, self.alice, text='same')
        create_message(self.group, self.alice, text='same')

        self.assertEqual(Message.objects.filter(conversation=self.group).count(), 2)

    def test_the_same_key_in_another_group_is_a_different_message(self):
        # The key is the SENDER's, scoped to a conversation. A client numbering
        # its sends per-thread must not have one group swallow another's.
        other = Conversation.start_group(self.alice, name='Work')

        here = create_message(self.group, self.alice, text='hi', client_id='1')
        there = create_message(other, self.alice, text='hi', client_id='1')

        self.assertNotEqual(here.pk, there.pk)

    def test_the_key_comes_back_so_a_client_can_match_its_own_message(self):
        create_message(self.group, self.alice, text='hello', client_id='xyz')

        data = self.assertSuccess(self.client.get(self.messages_url()))

        self.assertEqual(data['results'][0]['client_id'], 'xyz')


class BroadcastModeTests(EnvelopeAssertions, APITestCase):
    """A group where admins announce and members receive.

    Enforced in `create_message`, the single write path, so REST, the websocket
    and inbound SMS all obey it without each remembering to.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob', phone='+639170000002')
        self.group = Conversation.start_group(self.alice, name='Announcements')
        self.group.add_participant(self.bob)
        self.group.mode = Conversation.Mode.BROADCAST
        self.group.save(update_fields=['mode'])
        self.client.force_authenticate(self.alice)

    def messages_url(self):
        return reverse('chat:conversation-messages', args=[self.group.id])

    def test_an_admin_can_post(self):
        self.assertSuccess(
            self.client.post(self.messages_url(), {'text': 'Office closed Monday'}),
            status.HTTP_201_CREATED,
        )

    def test_a_member_cannot_post(self):
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.post(self.messages_url(), {'text': 'why not'}),
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(Message.objects.count(), 0)

    def test_a_member_can_still_read(self):
        # Broadcast restricts speaking, not membership.
        self.client.post(self.messages_url(), {'text': 'Office closed Monday'})
        self.client.force_authenticate(self.bob)
        data = self.assertSuccess(self.client.get(self.messages_url()))
        self.assertEqual(
            [m['text'] for m in data['results']], ['Office closed Monday']
        )

    def test_a_member_texting_in_is_told_rather_than_ignored(self):
        # They are a member in good standing; silence would read as breakage.
        result = inbound.handle_inbound(self.group, '+639170000002', 'why not')
        self.assertEqual(result.action, 'refused_broadcast')
        self.assertIn('announcements only', result.reply)
        self.assertEqual(Message.objects.count(), 0)

    def test_an_admin_texting_in_still_posts(self):
        self.alice.profile.phone_number = '+639170000001'
        self.alice.profile.phone_no_verified = True
        self.alice.profile.save()

        result = inbound.handle_inbound(self.group, '+639170000001', 'Office closed')
        self.assertEqual(result.action, 'posted')

    def test_switching_back_to_chat_lets_members_post_again(self):
        self.assertSuccess(
            self.client.patch(
                reverse('chat:conversation-settings', args=[self.group.id]),
                {'settings': {'mode': 'chat'}},
                format='json',
            )
        )
        self.client.force_authenticate(self.bob)
        self.assertSuccess(
            self.client.post(self.messages_url(), {'text': 'thanks'}),
            status.HTTP_201_CREATED,
        )

    def test_a_member_still_cannot_post_over_the_websocket(self):
        # The check is at the write path, so every entry point inherits it.
        from chat.models import PostingNotAllowed
        from chat.services import broadcast_message_updated, create_message

        with self.assertRaises(PostingNotAllowed):
            create_message(conversation=self.group, sender=self.bob, text='sneaking in')


class SplitIdentityTests(EnvelopeAssertions, APITestCase):
    """One human holding two rows - an account and a phone number.

    Every bug here was a case where the two halves of one person were treated
    as two people. The earlier tests only ever built a single row, which is
    exactly why they passed while blocking was bypassable.
    """

    def setUp(self):
        # The inbound rate limits are cache-backed and shared across tests.
        cache.clear()
        self.alice = make_user('alice')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.join_mode = Conversation.JoinMode.OPEN
        self.group.save(update_fields=['join_mode'])
        self.number = '+639170000002'

    def two_rows_for_bob(self):
        """Bob as an account AND as a phone number - a reachable state.

        He joins in the app while his number is still unverified (so nothing
        links the two), texts JOIN from that handset, then verifies the number.
        """
        bob = make_user('bob')
        self.group.add_participant(bob)
        inbound.handle_inbound(self.group, self.number, 'JOIN')

        bob.profile.phone_number = self.number
        bob.profile.phone_no_verified = True
        bob.profile.save()
        return bob

    def test_blocking_one_row_blocks_the_person(self):
        # resolve_sender used to return the first ACTIVE row while the REST
        # path resolved BLOCKED - two answers to one question, so a blocked
        # member kept posting by SMS.
        bob = self.two_rows_for_bob()
        account_row = Participant.objects.get(conversation=self.group, user=bob)
        membership.remove(account_row, self.alice, block=True)

        result = inbound.handle_inbound(self.group, self.number, 'still here')

        self.assertEqual(result.action, 'ignored_unknown_sender')
        self.assertEqual(Message.objects.count(), 0)

    def test_a_banned_account_cannot_text_its_way_to_a_fresh_membership(self):
        # The ban check used to live inside the identity lookup, so a banned
        # person stopped being findable, fell through, and was handed a brand
        # new row - blocking failed OPEN.
        bob = make_user('bob', phone=self.number)
        row = self.group.add_participant(bob)
        membership.remove(row, self.alice, block=True)
        User.objects.filter(pk=bob.pk).update(is_active=False)

        result = inbound.handle_inbound(self.group, self.number, 'JOIN')

        self.assertEqual(result.action, 'refused_blocked')
        self.assertEqual(self.group.participants.count(), 2)

    def test_a_banned_account_cannot_post_even_while_active(self):
        bob = make_user('bob', phone=self.number)
        self.group.add_participant(bob)
        User.objects.filter(pk=bob.pk).update(is_active=False)

        result = inbound.handle_inbound(self.group, self.number, 'hello')

        self.assertEqual(result.action, 'ignored_banned_account')
        self.assertEqual(Message.objects.count(), 0)

    def test_approving_refuses_somebody_blocked_under_their_other_identity(self):
        # The admin reviewing the queue cannot see that this pending account is
        # the same human as a blocked phone row.
        self.group.join_mode = Conversation.JoinMode.APPROVAL
        self.group.save(update_fields=['join_mode'])

        inbound.handle_inbound(self.group, self.number, 'JOIN')
        phone_row = Participant.objects.get(
            conversation=self.group, phone_number=self.number
        )
        membership.remove(phone_row, self.alice, block=True)

        # She asks while her number is still UNVERIFIED, so nothing links the
        # request to the blocked phone row and a PENDING account row is made.
        mom = make_user('mom')
        pending = membership.request_to_join(self.group, user=mom)
        self.assertEqual(pending.status, Participant.Status.PENDING)

        # She verifies it while waiting. Now the two rows are the same person -
        # and the admin looking at the queue has no way to see that.
        mom.profile.phone_number = self.number
        mom.profile.phone_no_verified = True
        mom.profile.save()

        with self.assertRaises(BlockedFromConversation):
            membership.approve(pending, self.alice)
        pending.refresh_from_db()
        self.assertEqual(pending.status, Participant.Status.PENDING)

    def test_a_blocked_person_is_refused_at_the_join_itself_when_linkable(self):
        # When the identifiers already line up, the refusal happens earlier and
        # no pending row is created for an admin to mishandle at all.
        self.group.join_mode = Conversation.JoinMode.APPROVAL
        self.group.save(update_fields=['join_mode'])
        inbound.handle_inbound(self.group, self.number, 'JOIN')
        membership.remove(
            Participant.objects.get(
                conversation=self.group, phone_number=self.number
            ),
            self.alice,
            block=True,
        )

        mom = make_user('mom', phone=self.number)
        with self.assertRaises(BlockedFromConversation):
            membership.request_to_join(self.group, user=mom)

    def test_joining_by_account_claims_the_old_phone_row(self):
        # It used to reactivate the phone-only row and never attach the account,
        # so the caller was told "joined" while has_participant stayed False.
        phone_row = self.group.add_phone_participant(
            self.number, display_name='Mom', added_by=self.alice
        )
        membership.remove(phone_row, self.alice)

        mom = make_user('mom', phone=self.number)
        self.client.force_authenticate(mom)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-join'),
                {'code': self.group.join_code},
                format='json',
            )
        )

        self.assertTrue(self.group.has_participant(mom))
        self.assertEqual(self.group.participants.filter(user=mom).count(), 1)

    def test_a_recycled_number_does_not_collide_with_its_old_row(self):
        # Mom's row keeps the number she surrendered; the carrier gives it to a
        # stranger, who texts JOIN. That used to raise IntegrityError -> 500.
        mom = make_user('mom', phone=self.number)
        self.group.add_participant(mom)
        mom.profile.phone_number = '+639170000009'
        mom.profile.phone_no_verified = True
        mom.profile.save()
        Participant.objects.filter(conversation=self.group, user=mom).update(
            phone_number=self.number
        )

        result = inbound.handle_inbound(self.group, self.number, 'JOIN')

        self.assertEqual(result.action, 'joined')
        self.assertNotEqual(result.participant.user_id, mom.pk)

    def test_a_pending_joiner_asking_again_is_answered(self):
        self.group.join_mode = Conversation.JoinMode.APPROVAL
        self.group.save(update_fields=['join_mode'])
        inbound.handle_inbound(self.group, self.number, 'JOIN')

        result = inbound.handle_inbound(self.group, self.number, 'JOIN')

        self.assertEqual(result.action, 'join_pending')
        self.assertIn('still with the admins', result.reply)

    def test_the_sole_admin_texting_leave_is_told_not_crashed(self):
        # An uncaught raise here 500s the webhook, and a carrier reads 500 as
        # "retry" - so the same text arrives again and again.
        alice = make_user('solo', phone='+639170000007')
        group = Conversation.start_group(alice, name='Solo')

        result = inbound.handle_inbound(group, '+639170000007', 'LEAVE')

        self.assertEqual(result.action, 'refused')
        self.assertIn('admin', result.reply)
        self.assertTrue(group.has_participant(alice))


class GroupAdminActionsTests(EnvelopeAssertions, APITestCase):
    """Deleting a group, and naming a member who has no account."""

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', phone='+639170000001')
        self.bob = make_user('bob', phone='+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.client.force_authenticate(self.alice)

    def detail_url(self):
        return reverse('chat:conversation-detail', args=[self.group.id])

    def rename_url(self):
        return reverse('chat:conversation-rename', args=[self.group.id])

    # --- deleting -----------------------------------------------------------

    def test_an_admin_can_delete_the_group(self):
        response = self.client.delete(self.detail_url())
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Conversation.objects.filter(pk=self.group.pk).exists())

    def test_a_plain_member_cannot_delete_it(self):
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.delete(self.detail_url()), status.HTTP_403_FORBIDDEN
        )
        self.assertTrue(Conversation.objects.filter(pk=self.group.pk).exists())

    def test_a_stranger_gets_a_404_rather_than_a_403(self):
        # A non-member is not told the conversation exists.
        carol = make_user('carol')
        self.client.force_authenticate(carol)
        self.assertFailure(
            self.client.delete(self.detail_url()), status.HTTP_404_NOT_FOUND
        )

    def test_a_one_to_one_conversation_cannot_be_deleted(self):
        # It belongs to two people equally; neither can end it for both.
        direct, _ = Conversation.start_direct(self.alice, self.bob)
        self.assertFailure(
            self.client.delete(
                reverse('chat:conversation-detail', args=[direct.id])
            )
        )

    # --- naming a phone-only member -----------------------------------------

    def test_an_admin_names_a_member_who_joined_by_text(self):
        mom = self.group.add_phone_participant('+639171111111', display_name='+63…1111')
        data = self.assertSuccess(
            self.client.post(
                self.rename_url(),
                {'participant': mom.pk, 'display_name': 'Mom'},
                format='json',
            )
        )
        self.assertEqual(data['display_label'], 'Mom')

    def test_a_member_with_an_account_keeps_their_own_name(self):
        # Otherwise an admin could relabel another person in the group.
        bob_row = Participant.objects.get(conversation=self.group, user=self.bob)
        self.assertFailure(
            self.client.post(
                self.rename_url(),
                {'participant': bob_row.pk, 'display_name': 'Not Bob'},
                format='json',
            )
        )

    def test_a_plain_member_cannot_rename_anybody(self):
        mom = self.group.add_phone_participant('+639171111111', display_name='Mom')
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.post(
                self.rename_url(),
                {'participant': mom.pk, 'display_name': 'Hijacked'},
                format='json',
            ),
            status.HTTP_403_FORBIDDEN,
        )


class GroupCreationWithSettingsTests(EnvelopeAssertions, APITestCase):
    """One request: members, numbers to invite, and the group's policy."""

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', phone='+639170000001')
        self.bob = make_user('bob', phone='+639170000002')
        self.client.force_authenticate(self.alice)

    def create(self, **extra):
        payload = {'name': 'Family', 'description': 'Home things'}
        payload.update(extra)
        return self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'), payload, format='json'
            ),
            status.HTTP_201_CREATED,
        )

    def test_everything_in_one_request(self):
        data = self.create(
            members=['bob'],
            invite=[{'phone_number': '+639171111111', 'display_name': 'Mom'}],
            settings={
                'mode': 'broadcast',
                'join_mode': 'code',
                'add_member_permission': 'members',
                'sms_replies_enabled': False,
            },
        )
        group = Conversation.objects.get(pk=data['id'])

        self.assertEqual(group.mode, Conversation.Mode.BROADCAST)
        self.assertEqual(group.join_mode, Conversation.JoinMode.CODE)
        self.assertEqual(group.add_member_permission, 'members')
        self.assertFalse(group.sms_replies_enabled)

        labels = {p.display_label for p in group.active_participants}
        self.assertEqual(
            labels,
            {self.alice.profile.display_label, self.bob.profile.display_label, 'Mom'},
        )

    def test_settings_are_optional_and_default(self):
        group = Conversation.objects.get(pk=self.create()['id'])
        self.assertEqual(group.mode, Conversation.Mode.CHAT)
        self.assertEqual(group.join_mode, Conversation.JoinMode.INVITE)
        self.assertTrue(group.sms_delivery_enabled)

    def test_an_invited_number_is_a_member_but_not_yet_consented(self):
        data = self.create(
            invite=[{'phone_number': '+639171111111', 'display_name': 'Mom'}]
        )
        mom = Participant.objects.get(
            conversation_id=data['id'], phone_number='+639171111111'
        )
        self.assertEqual(mom.status, Participant.Status.ACTIVE)
        self.assertEqual(sms_consent_of(mom), ChannelConsent.Status.PENDING)

    def test_an_unreachable_invite_does_not_lose_the_group(self):
        # The group and its real members are already committed; losing them
        # because one number failed helps nobody.
        with mock.patch('chat.membership.invite_by_phone',
                        side_effect=RuntimeError('provider down')):
            data = self.create(
                members=['bob'],
                invite=[{'phone_number': '+639171111111', 'display_name': 'Mom'}],
            )
        group = Conversation.objects.get(pk=data['id'])
        self.assertTrue(group.has_participant(self.bob))

    def test_a_bad_number_is_rejected_before_anything_is_created(self):
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Family',
                 'invite': [{'phone_number': '09171234567', 'display_name': 'Mom'}]},
                format='json',
            )
        )
        self.assertFalse(Conversation.objects.filter(name='Family').exists())


class GroupPermissionSettingsTests(EnvelopeAssertions, APITestCase):
    """add_member_permission and edit_group_permission, once enforced."""

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', phone='+639170000001')
        self.bob = make_user('bob', phone='+639170000002')
        self.carol = make_user('carol', phone='+639170000003')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)

    def settings_url(self):
        return reverse('chat:conversation-settings', args=[self.group.id])

    def set(self, **fields):
        for k, v in fields.items():
            setattr(self.group, k, v)
        self.group.save(update_fields=list(fields))

    # --- who may add people -------------------------------------------------

    def test_by_default_only_an_admin_adds_people(self):
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.post(
                reverse('chat:conversation-members', args=[self.group.id]),
                {'members': ['carol']}, format='json',
            ),
            status.HTTP_403_FORBIDDEN,
        )

    def test_members_may_add_when_the_group_says_so(self):
        self.set(add_member_permission='members')
        self.client.force_authenticate(self.bob)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-members', args=[self.group.id]),
                {'members': ['carol']}, format='json',
            )
        )
        self.assertTrue(self.group.has_participant(self.carol))

    def test_the_same_rule_covers_inviting_by_number(self):
        # Inviting a number IS adding somebody.
        self.set(add_member_permission='members')
        self.client.force_authenticate(self.bob)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-invite', args=[self.group.id]),
                {'phone_number': '+639171111111', 'display_name': 'Mom'},
                format='json',
            ),
            status.HTTP_201_CREATED,
        )

    # --- who may edit the group --------------------------------------------

    def test_by_default_only_an_admin_edits_the_group(self):
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.patch(self.settings_url(), {'name': 'Renamed'},
                              format='json'),
            status.HTTP_403_FORBIDDEN,
        )

    def test_members_may_rename_when_the_group_says_so(self):
        self.set(edit_group_permission='members')
        self.client.force_authenticate(self.bob)
        self.assertSuccess(
            self.client.patch(self.settings_url(), {'name': 'Renamed'},
                              format='json')
        )

    def test_but_a_member_can_never_change_the_policy_settings(self):
        # Otherwise a member allowed to rename the group could simply grant
        # themselves everything else.
        self.set(edit_group_permission='members')
        self.client.force_authenticate(self.bob)
        self.assertFailure(
            self.client.patch(
                self.settings_url(),
                {'settings': {'add_member_permission': 'members'}},
                format='json',
            ),
            status.HTTP_403_FORBIDDEN,
        )
        self.group.refresh_from_db()
        self.assertEqual(self.group.add_member_permission, 'admins')

class EmailInvitationTests(EnvelopeAssertions, APITransactionTestCase):
    """Adding somebody to a group by an address they may not have registered.

    TransactionTestCase, like InviteAndNotifyTests: the invitation is sent from
    a transaction.on_commit callback, which never fires inside a TestCase whose
    transaction is rolled back.

    Two outcomes from one input, decided by the address rather than by the
    caller: a VERIFIED account is added outright, and anybody else is emailed
    and becomes a member only when they prove they read that mailbox.

    Nothing in the invitation is a credential - the link carries them to the
    signup form and no further - so a forwarded invitation gives the reader
    nothing at all.
    """

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username='yul', password='x')
        self.group = Conversation.start_group(self.admin, name='Family HQ')

    def a_verified_account(self, username, email):
        user = User.objects.create_user(username=username, email=email, password='x')
        user.profile.email_verified = True
        user.profile.save(update_fields=['email_verified'])
        return user

    # --- the address belongs to somebody -------------------------------

    def test_a_verified_account_is_added_outright(self):
        mara = self.a_verified_account('mara', 'mara@example.com')

        with mock.patch('utils.mail.send_templated_email') as sent:
            row = membership.invite_by_email(self.group, 'mara@example.com', self.admin)

        self.assertEqual(row.user, mara)
        self.assertEqual(row.status, Participant.Status.ACTIVE)
        self.assertIsNone(row.email)
        sent.assert_not_called()

    def test_matching_an_account_ignores_case(self):
        mara = self.a_verified_account('mara', 'mara@example.com')
        row = membership.invite_by_email(self.group, 'MARA@Example.COM', self.admin)
        self.assertEqual(row.user, mara)

    def test_an_unverified_address_is_invited_not_added(self):
        """The squat, again. An unproven address is not that person."""
        squatter = User.objects.create_user(
            username='mallory', email='finance@acme.com', password='x'
        )
        squatter.profile.email_verified = False
        squatter.profile.save(update_fields=['email_verified'])

        with mock.patch('utils.mail.send_templated_email'):
            row = membership.invite_by_email(self.group, 'finance@acme.com', self.admin)

        self.assertIsNone(row.user)
        self.assertEqual(row.email, 'finance@acme.com')
        self.assertFalse(
            Participant.objects.filter(conversation=self.group, user=squatter).exists()
        )

    # --- nobody has the address yet ------------------------------------

    def test_an_unknown_address_creates_a_row_and_emails_them(self):
        with mock.patch('utils.mail.send_templated_email') as sent:
            row = membership.invite_by_email(self.group, 'Ana@Example.com', self.admin)

        self.assertIsNone(row.user)
        self.assertEqual(row.email, 'ana@example.com')
        self.assertTrue(row.is_email_only)
        self.assertFalse(row.is_phone_only)
        sent.assert_called_once()
        context = sent.call_args.kwargs['context']
        self.assertEqual(context['group_name'], 'Family HQ')
        self.assertIn('ana%40example.com', context['join_url'])

    def test_the_label_is_the_local_part_not_the_address(self):
        # It goes out to everybody in the group; the full address must not.
        with mock.patch('utils.mail.send_templated_email'):
            row = membership.invite_by_email(self.group, 'ana@example.com', self.admin)
        self.assertEqual(row.display_label, 'ana')

    def test_re_inviting_sends_again_without_a_second_row(self):
        with mock.patch('utils.mail.send_templated_email') as sent:
            first = membership.invite_by_email(self.group, 'ana@example.com', self.admin)
            second = membership.invite_by_email(self.group, 'ana@example.com', self.admin)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(sent.call_count, 2)
        self.assertEqual(
            Participant.objects.filter(conversation=self.group, email='ana@example.com').count(), 1
        )

    def test_a_blocked_address_cannot_be_re_invited(self):
        with mock.patch('utils.mail.send_templated_email'):
            row = membership.invite_by_email(self.group, 'ana@example.com', self.admin)
        row.status = Participant.Status.BLOCKED
        row.save(update_fields=['status'])

        with self.assertRaises(BlockedFromConversation):
            membership.invite_by_email(self.group, 'ana@example.com', self.admin)

    def test_a_non_admin_cannot_invite(self):
        outsider = User.objects.create_user(username='nobody', password='x')
        with self.assertRaises(membership.JoinRefused):
            membership.invite_by_email(self.group, 'ana@example.com', outsider)

    def test_a_failed_send_does_not_undo_the_row(self):
        from utils.mail import EmailError

        with mock.patch('utils.mail.send_templated_email',
                        side_effect=EmailError('resend is down')):
            row = membership.invite_by_email(self.group, 'ana@example.com', self.admin)

        self.assertTrue(Participant.objects.filter(pk=row.pk).exists())

    # --- claiming on verification ---------------------------------------

    def claim(self, email):
        user = User.objects.create_user(username='ana', email=email, password='x')
        user.profile.email_verified = True
        user.profile.save(update_fields=['email_verified'])
        return user, membership.claim_email_invitations(user)

    def test_verifying_the_address_turns_the_invitation_into_membership(self):
        with mock.patch('utils.mail.send_templated_email'):
            row = membership.invite_by_email(self.group, 'ana@example.com', self.admin)

        user, claimed = self.claim('ana@example.com')

        self.assertEqual(claimed, 1)
        row.refresh_from_db()
        self.assertEqual(row.user, user)
        self.assertIsNone(row.email)
        self.assertEqual(row.status, Participant.Status.ACTIVE)

    def test_an_unverified_account_claims_nothing(self):
        with mock.patch('utils.mail.send_templated_email'):
            membership.invite_by_email(self.group, 'ana@example.com', self.admin)

        user = User.objects.create_user(
            username='ana', email='ana@example.com', password='x'
        )
        self.assertEqual(membership.claim_email_invitations(user), 0)

    def test_a_blocked_invitation_is_never_claimed(self):
        with mock.patch('utils.mail.send_templated_email'):
            row = membership.invite_by_email(self.group, 'ana@example.com', self.admin)
        row.status = Participant.Status.BLOCKED
        row.save(update_fields=['status'])

        _, claimed = self.claim('ana@example.com')

        self.assertEqual(claimed, 0)
        row.refresh_from_db()
        self.assertEqual(row.status, Participant.Status.BLOCKED)
        self.assertIsNone(row.user)

    def test_claiming_merges_with_an_account_row_that_already_exists(self):
        user = self.a_verified_account('ana', 'ana@example.com')
        # Invited by address BEFORE the account verified, then added directly.
        row = Participant.objects.create(
            conversation=self.group, email='ana@example.com',
            display_name='ana', role=Participant.Role.MEMBER,
        )
        account_row = self.group.add_participant(user)

        claimed = membership.claim_email_invitations(user)

        self.assertEqual(claimed, 1)
        self.assertFalse(Participant.objects.filter(pk=row.pk).exists())
        self.assertTrue(Participant.objects.filter(pk=account_row.pk).exists())
        self.assertEqual(
            Participant.objects.filter(conversation=self.group, user=user).count(), 1
        )

    def test_claiming_spans_every_group_they_were_invited_to(self):
        other = Conversation.start_group(self.admin, name='Book Club')
        with mock.patch('utils.mail.send_templated_email'):
            membership.invite_by_email(self.group, 'ana@example.com', self.admin)
            membership.invite_by_email(other, 'ana@example.com', self.admin)

        _, claimed = self.claim('ana@example.com')
        self.assertEqual(claimed, 2)

    # --- through the API ------------------------------------------------

    def test_creating_a_group_invites_the_addresses_given(self):
        self.client.force_authenticate(self.admin)
        with mock.patch('utils.mail.send_templated_email') as sent:
            response = self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'invite_email': ['ana@example.com']},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = Conversation.objects.get(name='Trip')
        self.assertTrue(
            Participant.objects.filter(conversation=created, email='ana@example.com').exists()
        )
        sent.assert_called_once()

    def test_an_unknown_address_does_not_fail_the_whole_request(self):
        """Unlike `members`, which is all-or-nothing on purpose."""
        self.client.force_authenticate(self.admin)
        with mock.patch('utils.mail.send_templated_email'):
            response = self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Trip', 'invite_email': ['nobody@example.com']},
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

class SettingsChangeBroadcastTests(EnvelopeAssertions, APITestCase):
    """Changing a group's settings has to reach the people looking at it.

    `mode` decides which list a client files the group under and whether it
    offers a compose box at all. Before this event only the admin who made the
    change saw it: every other member kept a Broadcast group filed under Chat,
    with a compose box that the server would refuse.
    """

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username='yul', password='x')
        self.member = User.objects.create_user(username='yul_2', password='x')
        self.group = Conversation.start_group(self.admin, name='Pickle 5')
        self.group.add_participant(self.member)
        self.client.force_authenticate(self.admin)
        self.url = reverse('chat:conversation-settings', args=[self.group.pk])

    def patch_settings(self, settings_payload):
        with mock.patch('chat.serializers.broadcast_conversation_updated') as sent:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.patch(
                    self.url, {'settings': settings_payload}, format='json'
                )
        return response, sent

    def test_switching_to_broadcast_is_announced(self):
        response, sent = self.patch_settings({'mode': 'broadcast'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.group.refresh_from_db()
        self.assertEqual(self.group.mode, Conversation.Mode.BROADCAST)
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[0].pk, self.group.pk)

    def test_the_response_carries_the_new_mode(self):
        # The admin's own client updates from this payload rather than waiting
        # for its own broadcast to come back.
        response, _ = self.patch_settings({'mode': 'broadcast'})
        self.assertEqual(response.json()['data']['settings']['mode'], 'broadcast')

    def test_switching_back_is_announced_too(self):
        self.group.mode = Conversation.Mode.BROADCAST
        self.group.save(update_fields=['mode'])

        response, sent = self.patch_settings({'mode': 'chat'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.group.refresh_from_db()
        self.assertEqual(self.group.mode, Conversation.Mode.CHAT)
        sent.assert_called_once()

    def test_a_join_mode_change_is_announced(self):
        _, sent = self.patch_settings({'join_mode': 'open'})
        sent.assert_called_once()

    def test_nothing_is_announced_when_nothing_changed(self):
        with mock.patch('chat.serializers.broadcast_conversation_updated') as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.patch(self.url, {}, format='json')
        sent.assert_not_called()

    def test_broadcast_mode_actually_refuses_a_member_posting(self):
        # The event is cosmetic unless the rule behind it holds.
        self.patch_settings({'mode': 'broadcast'})
        self.group.refresh_from_db()

        member_row = Participant.objects.get(conversation=self.group, user=self.member)
        admin_row = Participant.objects.get(conversation=self.group, user=self.admin)
        self.assertFalse(self.group.allows_posting_by(member_row))
        self.assertTrue(self.group.allows_posting_by(admin_row))



class ConversationStarterSubscriptionTests(EnvelopeAssertions, APITestCase):
    """Whoever starts a conversation has a socket that is in it.

    A socket joins a conversation when it connects, or when it hears
    `conversation.added`. Only the people ADDED used to be sent that event, so
    the starter of a direct message or the creator of a group was never in the
    conversation they had just made. Their own messages still showed - the REST
    response carries them - and the other side's replies, typing and read
    receipts did not, until they reconnected.
    """

    def setUp(self):
        self.alice = make_user('alice', '+639170000101')
        self.bob = make_user('bob', '+639170000102')
        self.client.force_authenticate(self.alice)

    def start(self, url_name, payload):
        """Run one request with its after-commit work, recording who was told."""
        with mock.patch('chat.services.broadcast_added_to_conversation') as sent, \
                mock.patch('notification.services.notify_many') as notified:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse(url_name), payload, format='json'
                )
        subscribed = {
            user.pk for call in sent.call_args_list for user in call.args[1]
        }
        told = {
            user.pk for call in notified.call_args_list for user in call.args[0]
        }
        return response, subscribed, told

    def test_starting_a_direct_message_subscribes_both_people(self):
        response, subscribed, _ = self.start(
            'chat:conversation-direct', {'identifier': 'bob'}
        )

        self.assertSuccess(response)
        self.assertEqual(subscribed, {self.alice.pk, self.bob.pk})

    def test_the_starter_is_not_notified_that_they_were_added(self):
        _, _, told = self.start('chat:conversation-direct', {'identifier': 'bob'})

        self.assertEqual(told, {self.bob.pk})

    def test_opening_an_existing_direct_message_tells_nobody(self):
        # Both sockets already joined it - at connect, or when it was made.
        self.start('chat:conversation-direct', {'identifier': 'bob'})

        response, subscribed, told = self.start(
            'chat:conversation-direct', {'identifier': 'bob'}
        )

        self.assertSuccess(response)
        self.assertEqual((subscribed, told), (set(), set()))

    def test_creating_a_group_subscribes_the_creator(self):
        response, subscribed, told = self.start(
            'chat:conversation-group', {'name': 'Trip', 'members': ['bob']}
        )

        self.assertSuccess(response, status.HTTP_201_CREATED)
        self.assertEqual(subscribed, {self.alice.pk, self.bob.pk})
        # Bob was added by somebody else; Alice made it.
        self.assertEqual(told, {self.bob.pk})

    def test_a_group_with_nobody_else_in_it_still_subscribes_the_creator(self):
        response, subscribed, told = self.start(
            'chat:conversation-group', {'name': 'Just me'}
        )

        self.assertSuccess(response, status.HTTP_201_CREATED)
        self.assertEqual(subscribed, {self.alice.pk})
        self.assertEqual(told, set())


class UnreadCountTests(EnvelopeAssertions, APITestCase):
    """What "unread" means, and who it means it for.

    The count is one number per conversation per person, read off each
    member's own pointer - so a group is not a special case, it is the same
    thing more times. The cases that actually catch bugs are the asymmetric
    ones: your own messages, a member who has read nothing, and a pointer
    somebody tries to wind backwards.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.carol = make_user('carol')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.group.add_participant(self.carol)

    def read_url(self, conversation=None):
        return reverse(
            'chat:conversation-read', args=[(conversation or self.group).id]
        )

    def unread_for(self, user):
        self.client.force_authenticate(user)
        data = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-detail', args=[self.group.id])
            )
        )
        return data['unread_count']

    def pointer_of(self, user):
        return Participant.objects.get(
            conversation=self.group, user=user
        ).last_read_message_id

    # --- counting -----------------------------------------------------------

    def test_a_member_who_has_read_nothing_has_read_nothing(self):
        create_message(self.group, self.bob, text='one')
        create_message(self.group, self.bob, text='two')

        # A null pointer must mean "all of it", not "none of it" - the bug
        # would be a NULL comparison quietly reporting zero.
        self.assertEqual(self.unread_for(self.alice), 2)

    def test_your_own_messages_are_not_unread_to_you(self):
        create_message(self.group, self.alice, text='mine')
        create_message(self.group, self.bob, text='theirs')

        self.assertEqual(self.unread_for(self.alice), 1)

    def test_reading_clears_the_count(self):
        create_message(self.group, self.bob, text='one')
        create_message(self.group, self.bob, text='two')

        self.client.force_authenticate(self.alice)
        self.assertSuccess(self.client.post(self.read_url(), {}))

        self.assertEqual(self.unread_for(self.alice), 0)

    def test_messages_after_the_read_still_count(self):
        create_message(self.group, self.bob, text='seen')
        self.client.force_authenticate(self.alice)
        self.assertSuccess(self.client.post(self.read_url(), {}))

        create_message(self.group, self.bob, text='arrived after')

        self.assertEqual(self.unread_for(self.alice), 1)

    def test_reading_up_to_a_named_message_leaves_the_rest(self):
        first = create_message(self.group, self.bob, text='one')
        create_message(self.group, self.bob, text='two')
        create_message(self.group, self.bob, text='three')

        self.client.force_authenticate(self.alice)
        self.assertSuccess(
            self.client.post(self.read_url(), {'message_id': first.pk})
        )

        self.assertEqual(self.unread_for(self.alice), 2)

    def test_the_count_comes_back_on_the_list_too(self):
        create_message(self.group, self.bob, text='one')

        self.client.force_authenticate(self.alice)
        data = self.assertSuccess(
            self.client.get(reverse('chat:conversation-list'))
        )
        rows = data['results'] if isinstance(data, dict) else data

        self.assertEqual(rows[0]['unread_count'], 1)

    # --- the pointer only goes forward -------------------------------------

    def test_an_older_message_does_not_wind_the_pointer_back(self):
        first = create_message(self.group, self.bob, text='one')
        create_message(self.group, self.bob, text='two')

        self.client.force_authenticate(self.alice)
        self.assertSuccess(self.client.post(self.read_url(), {}))
        ahead = self.pointer_of(self.alice)

        # A tab that was asleep reports where it had got to. Honouring that
        # would turn messages Alice has read back into unread ones, on every
        # device she has open.
        self.assertSuccess(
            self.client.post(self.read_url(), {'message_id': first.pk})
        )

        self.assertEqual(self.pointer_of(self.alice), ahead)
        self.assertEqual(self.unread_for(self.alice), 0)

    # --- groups -------------------------------------------------------------

    def test_each_member_of_a_group_counts_for_themselves(self):
        create_message(self.group, self.alice, text='hello all')

        self.client.force_authenticate(self.bob)
        self.assertSuccess(self.client.post(self.read_url(), {}))

        # Bob reading changes nothing for Carol.
        self.assertEqual(self.unread_for(self.bob), 0)
        self.assertEqual(self.unread_for(self.carol), 1)
        self.assertEqual(self.unread_for(self.alice), 0)

    def test_a_phone_only_member_does_not_disturb_anyone_else(self):
        # SMS can tell you a handset took a text, never that a person read it,
        # so these rows have no pointer and never will. They must not make the
        # count wrong for the members who do have one.
        self.group.add_phone_participant(
            '+639171234567', display_name='Mom', added_by=self.alice
        )
        create_message(self.group, self.bob, text='one')

        self.assertEqual(self.unread_for(self.alice), 1)

    # --- refusals -----------------------------------------------------------

    def test_a_message_from_another_conversation_is_refused(self):
        other = Conversation.start_group(self.alice, name='Work')
        stray = create_message(other, self.alice, text='elsewhere')

        self.client.force_authenticate(self.alice)
        response = self.client.post(
            self.read_url(), {'message_id': stray.pk}
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIsNone(self.pointer_of(self.alice))

    def test_a_non_member_cannot_mark_anything_read(self):
        outsider = make_user('mallory')
        self.client.force_authenticate(outsider)

        response = self.client.post(self.read_url(), {})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_reading_an_empty_conversation_is_harmless(self):
        empty = Conversation.start_group(self.alice, name='Quiet')

        self.client.force_authenticate(self.alice)
        self.assertSuccess(self.client.post(self.read_url(empty), {}))


class ArchiveAndMuteTests(EnvelopeAssertions, APITestCase):
    """Two switches that look similar and are not.

    Mute stops delivery on every channel, SMS included. Archive stops nothing -
    it only decides which of the member's own lists a conversation sits in.
    Conflating them is how somebody tidies their list and then misses the texts
    still arriving on their phone, so the difference is what these hold.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.client.force_authenticate(self.alice)

    def archive_url(self):
        return reverse('chat:conversation-archive', args=[self.group.id])

    def detail(self, user=None):
        self.client.force_authenticate(user or self.alice)
        return self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-detail', args=[self.group.id])
            )
        )

    def alice_row(self):
        return Participant.objects.get(conversation=self.group, user=self.alice)

    # --- archive ------------------------------------------------------------

    def test_archiving_shows_on_my_copy(self):
        self.assertSuccess(
            self.client.post(self.archive_url(), {'archived': True},
                             format='json')
        )

        self.assertTrue(self.detail()['archived'])

    def test_archiving_is_mine_alone(self):
        self.assertSuccess(
            self.client.post(self.archive_url(), {'archived': True},
                             format='json')
        )

        self.assertFalse(self.detail(self.bob)['archived'])

    def test_unarchiving(self):
        self.client.post(self.archive_url(), {'archived': True}, format='json')
        self.assertSuccess(
            self.client.post(self.archive_url(), {'archived': False},
                             format='json')
        )

        self.assertFalse(self.detail()['archived'])

    def test_a_new_message_brings_it_back(self):
        self.client.post(self.archive_url(), {'archived': True}, format='json')

        create_message(self.group, self.bob, text='still here?')

        self.assertFalse(self.detail()['archived'])

    def test_but_not_for_somebody_who_muted_it(self):
        # They said they did not want to hear about this group. Pulling it back
        # to the top of their list on every post is hearing about it.
        membership.set_muted(self.alice_row(), True)
        self.client.post(self.archive_url(), {'archived': True}, format='json')

        create_message(self.group, self.bob, text='still here?')

        self.assertTrue(self.detail()['archived'])

    def test_archiving_does_not_leave_the_group(self):
        self.client.post(self.archive_url(), {'archived': True}, format='json')

        self.assertEqual(self.alice_row().status, Participant.Status.ACTIVE)

    def test_a_non_member_cannot_archive(self):
        self.client.force_authenticate(make_user('mallory'))

        response = self.client.post(
            self.archive_url(), {'archived': True}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # --- mute ---------------------------------------------------------------

    def test_muting_shows_on_my_copy(self):
        membership.set_muted(self.alice_row(), True)

        self.assertTrue(self.detail()['muted'])

    def test_muting_is_mine_alone(self):
        membership.set_muted(self.alice_row(), True)

        self.assertFalse(self.detail(self.bob)['muted'])

    def test_archiving_does_not_mute(self):
        # The distinction the whole feature turns on: a tidied list must not
        # quietly stop the texts.
        self.client.post(self.archive_url(), {'archived': True}, format='json')

        self.assertFalse(self.detail()['muted'])
        self.assertFalse(self.alice_row().muted)


class ClearHistoryTests(EnvelopeAssertions, APITestCase):
    """Deleting a chat for yourself without leaving it.

    Distinct from both neighbours: archive keeps the history and only moves the
    conversation between the member's own lists; leaving takes them out of the
    group for everybody. This empties one member's view, keeps their
    membership, and lets the conversation return by itself carrying only what
    arrived afterwards.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.client.force_authenticate(self.alice)

    def clear_url(self):
        return reverse('chat:conversation-clear', args=[self.group.id])

    def clear(self):
        return self.assertSuccess(self.client.post(self.clear_url(), {}))

    def history(self, user=None):
        self.client.force_authenticate(user or self.alice)
        data = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-messages', args=[self.group.id])
            )
        )
        rows = data['results'] if isinstance(data, dict) else data
        return [row['text'] for row in rows]

    def detail(self, user=None):
        self.client.force_authenticate(user or self.alice)
        return self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-detail', args=[self.group.id])
            )
        )

    # --- clearing -----------------------------------------------------------

    def test_the_history_is_empty_afterwards(self):
        create_message(self.group, self.bob, text='old one')
        create_message(self.group, self.bob, text='old two')

        self.clear()

        self.assertEqual(self.history(), [])

    def test_everybody_else_still_has_theirs(self):
        create_message(self.group, self.bob, text='old one')

        self.clear()

        self.assertEqual(self.history(self.bob), ['old one'])

    def test_the_member_has_not_left(self):
        create_message(self.group, self.bob, text='old one')

        self.clear()

        row = Participant.objects.get(conversation=self.group, user=self.alice)
        self.assertEqual(row.status, Participant.Status.ACTIVE)

    def test_it_drops_out_of_the_list_while_there_is_nothing_to_show(self):
        create_message(self.group, self.bob, text='old one')

        self.clear()

        self.assertTrue(self.detail()['cleared'])

    # --- and comes back -----------------------------------------------------

    def test_a_new_message_brings_it_back(self):
        create_message(self.group, self.bob, text='old one')
        self.clear()

        create_message(self.group, self.bob, text='new one')

        self.assertFalse(self.detail()['cleared'])

    def test_and_it_carries_only_what_arrived_after(self):
        create_message(self.group, self.bob, text='old one')
        self.clear()
        create_message(self.group, self.bob, text='new one')

        self.assertEqual(self.history(), ['new one'])

    def test_the_cleared_messages_are_not_unread(self):
        # Otherwise the conversation returns wearing a badge for messages the
        # member can no longer open.
        create_message(self.group, self.bob, text='old one')
        create_message(self.group, self.bob, text='old two')

        self.clear()

        self.assertEqual(self.detail()['unread_count'], 0)

    def test_only_messages_after_the_clear_count_as_unread(self):
        create_message(self.group, self.bob, text='old one')
        self.clear()
        create_message(self.group, self.bob, text='new one')

        self.assertEqual(self.detail()['unread_count'], 1)

    def test_the_resume_cursor_cannot_reach_cleared_messages(self):
        first = create_message(self.group, self.bob, text='old one')
        create_message(self.group, self.bob, text='old two')
        self.clear()
        create_message(self.group, self.bob, text='new one')

        self.client.force_authenticate(self.alice)
        data = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-messages', args=[self.group.id]),
                {'after_id': first.pk - 1},
            )
        )
        rows = data['results'] if isinstance(data, dict) else data

        self.assertEqual([row['text'] for row in rows], ['new one'])

    # --- refusals -----------------------------------------------------------

    def test_clearing_an_empty_conversation_hides_it_too(self):
        """The case the message pointer alone could not express.

        A group nobody has posted in has no newest message to point at, so the
        clear used to write nothing at all and the row came back on the next
        refresh - which is what somebody clearing a group they never used
        reports as broken. `Participant.cleared_at` is what answers it.
        """
        empty = Conversation.start_group(self.alice, name='Quiet')

        body = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-clear', args=[empty.id]), {}
            )
        )
        self.assertTrue(body['cleared'])

        listed = self.assertSuccess(
            self.client.get(reverse('chat:conversation-list'))
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        row = [r for r in rows if r['id'] == empty.id][0]
        self.assertTrue(row['cleared'])

    def test_an_emptied_conversation_comes_back_when_somebody_posts(self):
        empty = Conversation.start_group(self.alice, name='Quiet')
        empty.add_participant(self.bob)
        self.client.post(
            reverse('chat:conversation-clear', args=[empty.id]), {}
        )

        create_message(empty, self.bob, text='anybody there?')

        listed = self.assertSuccess(
            self.client.get(reverse('chat:conversation-list'))
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        row = [r for r in rows if r['id'] == empty.id][0]
        self.assertFalse(row['cleared'])
        # And it carries only what arrived after - there was nothing before.
        self.assertEqual(row['last_message']['text'], 'anybody there?')

    def test_clearing_an_empty_conversation_hides_nothing_from_anybody_else(self):
        empty = Conversation.start_group(self.alice, name='Quiet')
        empty.add_participant(self.bob)
        self.client.post(
            reverse('chat:conversation-clear', args=[empty.id]), {}
        )

        self.client.force_authenticate(self.bob)
        listed = self.assertSuccess(
            self.client.get(reverse('chat:conversation-list'))
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        row = [r for r in rows if r['id'] == empty.id][0]
        self.assertFalse(row['cleared'])

    def test_a_non_member_cannot_clear(self):
        self.client.force_authenticate(make_user('mallory'))

        response = self.client.post(self.clear_url(), {})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ReadReceiptTests(EnvelopeAssertions, APITestCase):
    """Showing a sender that their message has been seen.

    Read state is private until somebody asks to see it, and this is the ask:
    the pointer that clears the reader's own badge is also what tells a sender
    their message landed. What it must never do is imply anything about the
    members it cannot speak for.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)

    def members(self):
        self.client.force_authenticate(self.alice)
        return members_of(self.client, self.group.id)

    def row_for(self, username):
        for row in self.members():
            user = row.get('user') or {}
            if user.get('username') == username:
                return row
        return None

    def test_a_member_who_has_read_nothing_reports_nothing(self):
        create_message(self.group, self.alice, text='hello')

        self.assertIsNone(self.row_for('bob')['last_read_message'])

    def test_reading_is_visible_to_the_other_members(self):
        message = create_message(self.group, self.alice, text='hello')

        self.client.force_authenticate(self.bob)
        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-read', args=[self.group.id]), {}
            )
        )

        self.assertEqual(self.row_for('bob')['last_read_message'], message.pk)

    def test_a_phone_only_member_never_reports_a_read(self):
        # SMS can tell you a handset accepted a text and never that a person
        # read it. A receipt counting them would be inventing a fact.
        self.group.add_phone_participant(
            '+639171234567', display_name='Mom', added_by=self.alice
        )
        create_message(self.group, self.alice, text='hello')

        rows = [
            row for row in self.members() if row['display_label'] == 'Mom'
        ]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['last_read_message'])


class DeliveryEngineTests(EnvelopeAssertions, APITestCase):
    """The one ladder every channel asks before sending anything.

    Written against the engine rather than through SMS, because the whole
    reason it exists is that email and voice will ask it too - and a rule only
    the SMS tests cover is a rule the next channel can quietly get wrong.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )

    def post(self):
        return create_message(self.group, self.alice, text='Dinner at 7')

    def refusal(self):
        return delivery.refusal_reason(
            self.bob_row, ChannelConsent.Channel.SMS
        )

    # --- what stops delivery ------------------------------------------------

    def test_a_member_who_left_is_refused(self):
        self.bob_row.status = Participant.Status.LEFT
        self.bob_row.save(update_fields=['status'])

        self.assertEqual(self.refusal(), 'not an active member')

    def test_a_muted_member_is_refused(self):
        membership.set_muted(self.bob_row, True)

        self.assertEqual(self.refusal(), 'muted')

    def test_a_member_who_declined_is_refused(self):
        membership.set_channel_consent(
            self.bob_row,
            ChannelConsent.Channel.SMS,
            ChannelConsent.Status.DECLINED,
        )

        self.assertEqual(self.refusal(), 'declined this group')

    def test_a_member_who_opted_out_is_refused(self):
        membership.set_channel_consent(
            self.bob_row,
            ChannelConsent.Channel.SMS,
            ChannelConsent.Status.OPTED_OUT,
        )

        self.assertEqual(self.refusal(), 'opted out')

    def test_a_channel_switched_off_for_this_group_is_refused(self):
        self.bob_row.sms_enabled = False
        self.bob_row.save(update_fields=['sms_enabled'])

        self.assertEqual(self.refusal(), 'sms disabled for this group')

    # --- what does NOT stop delivery ----------------------------------------

    def test_archiving_does_not_stop_delivery(self):
        # The rule most easily got wrong, and the reason it is stated here
        # rather than left to each channel: archive hides a conversation from
        # a list. Muting is how somebody asks for silence.
        membership.set_archived(self.bob_row, True)

        self.assertIsNone(self.refusal())

    def test_an_ordinary_member_is_eligible(self):
        self.assertIsNone(self.refusal())

    # --- the audience -------------------------------------------------------

    def test_the_sender_is_never_in_their_own_audience(self):
        message = self.post()

        send_to, _ = delivery.eligible_for(
            message, ChannelConsent.Channel.SMS
        )

        self.assertNotIn(
            message.sender_participant_id, [row.pk for row in send_to]
        )

    def test_every_omission_carries_its_reason(self):
        membership.set_muted(self.bob_row, True)

        send_to, skipped = delivery.eligible_for(
            self.post(), ChannelConsent.Channel.SMS
        )

        self.assertEqual(send_to, [])
        self.assertEqual([reason for _, reason in skipped], ['muted'])

    def test_declining_one_channel_leaves_the_other_alone(self):
        # The whole reason the ladder is shared is that email and voice ask it
        # too - so the thing it could quietly get wrong is answering for the
        # wrong channel.
        membership.set_channel_consent(
            self.bob_row,
            ChannelConsent.Channel.EMAIL,
            ChannelConsent.Status.DECLINED,
        )

        self.assertEqual(
            delivery.refusal_reason(self.bob_row, ChannelConsent.Channel.EMAIL),
            'declined this group',
        )
        self.assertIsNone(self.refusal())

    def test_a_new_member_has_an_answer_on_record(self):
        # A missing row reads as a refusal, so a membership created by any
        # route without one would silently stop receiving anything.
        carol = make_user('carol')
        self.group.add_participant(carol)
        row = Participant.objects.get(conversation=self.group, user=carol)

        self.assertIsNotNone(
            membership.consent_status(row, ChannelConsent.Channel.SMS)
        )


class GroupEmailPreferenceTests(EnvelopeAssertions, APITestCase):
    """The member's own switch for one group's email.

    The only door that turns email back ON. The unsubscribe link printed in
    every group email can only ever subtract, because a forwarded link must not
    let a stranger undo somebody's decision - so this is where the other half
    of that lives.
    """

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
        self.client.force_authenticate(self.bob)

    def delivery(self, payload, conversation=None):
        return self.client.post(
            reverse(
                'chat:conversation-delivery',
                args=[(conversation or self.family).id],
            ),
            payload,
            format='json',
        )

    def email_consent(self, participant):
        return membership.consent_status(
            participant, ChannelConsent.Channel.EMAIL
        )

    def test_turning_email_off_declines_that_group(self):
        data = self.assertSuccess(self.delivery({'email_enabled': False}))

        self.assertFalse(data['email_enabled'])
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )

    def test_and_leaves_every_other_group_alone(self):
        self.delivery({'email_enabled': False})

        self.assertEqual(
            self.email_consent(self.bob_work), ChannelConsent.Status.OPTED_IN
        )

    def test_turning_it_back_on_opts_in_again(self):
        self.delivery({'email_enabled': False})

        data = self.assertSuccess(self.delivery({'email_enabled': True}))

        self.assertTrue(data['email_enabled'])
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.OPTED_IN
        )

    def test_it_will_not_clear_a_provider_level_opt_out(self):
        # OPTED_OUT is the provider's verdict - a spam complaint the mailbox
        # provider already acted on - and an app toggle must not overturn it,
        # exactly as only START clears an SMS stop.
        membership.set_channel_consent(
            self.bob_family,
            ChannelConsent.Channel.EMAIL,
            ChannelConsent.Status.OPTED_OUT,
        )

        self.delivery({'email_enabled': True})

        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.OPTED_OUT
        )

    def test_it_is_mine_alone(self):
        self.delivery({'email_enabled': False})

        alice_row = Participant.objects.get(
            conversation=self.family, user=self.alice
        )
        self.assertEqual(
            self.email_consent(alice_row), ChannelConsent.Status.OPTED_IN
        )

    def test_it_does_not_disturb_the_other_two_switches(self):
        membership.set_muted(self.bob_family, True)

        self.delivery({'email_enabled': False})

        self.bob_family.refresh_from_db()
        self.assertTrue(self.bob_family.muted)
        self.assertTrue(self.bob_family.sms_enabled)

    def test_it_leaves_sms_consent_alone(self):
        self.delivery({'email_enabled': False})

        self.assertEqual(
            membership.consent_status(
                self.bob_family, ChannelConsent.Channel.SMS
            ),
            ChannelConsent.Status.OPTED_IN,
        )

    def test_all_three_switches_can_travel_together(self):
        self.assertSuccess(
            self.delivery(
                {'muted': True, 'sms_enabled': False, 'email_enabled': False}
            )
        )

        self.bob_family.refresh_from_db()
        self.assertTrue(self.bob_family.muted)
        self.assertFalse(self.bob_family.sms_enabled)
        self.assertEqual(
            self.email_consent(self.bob_family), ChannelConsent.Status.DECLINED
        )

    def test_an_empty_body_is_still_refused(self):
        self.assertFailure(self.delivery({}))

    def test_a_non_member_gets_a_404(self):
        self.client.force_authenticate(make_user('mallory'))

        response = self.delivery({'email_enabled': False})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


import chat.models as chat_models


class BlockedDirectContactTests(EnvelopeAssertions, APITestCase):
    """A block stops direct contact and touches nothing else.

    Two halves, and the line between them is the feature. Direct contact really
    stops. A shared group really does not change - both stay members, both keep
    seeing each other's messages - and what stops there is only the external
    notification, which `BlockedGroupDeliveryTests` holds.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        # invite_by_email resolves through find_user, which only matches a
        # VERIFIED address - so without this the invite tests would exercise
        # the unclaimed-invitation branch instead of the one they name.
        for account in (self.alice, self.bob):
            account.profile.email_verified = True
            account.profile.save(update_fields=['email_verified'])
        self.client.force_authenticate(self.alice)

    def block(self, blocker, username):
        from user import blocks

        blocks.block(blocker, user=User.objects.get(username=username))

    def start_direct(self, identifier):
        return self.client.post(
            reverse('chat:conversation-direct'),
            {'identifier': identifier},
            format='json',
        )

    # --- starting one -------------------------------------------------------

    def test_the_blocker_cannot_start_a_direct_conversation(self):
        self.block(self.alice, 'bob')

        self.assertFailure(self.start_direct('bob'))

    def test_and_neither_can_the_blocked_person(self):
        self.block(self.alice, 'bob')
        self.client.force_authenticate(self.bob)

        self.assertFailure(self.start_direct('alice'))

    def test_the_blocker_is_told_plainly(self):
        # The one place the two sides of a block are deliberately treated
        # differently. A already knows they blocked B, so hiding it buys no
        # privacy and leaves them at a dead end that looks like a bug.
        self.block(self.alice, 'bob')

        refusal = self.assertFailure(self.start_direct('bob'))

        self.assertIn('You blocked this user', refusal['message'])

    def test_but_the_blocked_person_is_told_nothing(self):
        # For B the answer must be the one a stranger's typo gets, or the
        # refusal itself announces the block. Compared character for character
        # against a name that was never taken.
        self.block(self.alice, 'bob')
        self.client.force_authenticate(self.bob)

        blocked = self.assertFailure(self.start_direct('alice'))
        missing = self.assertFailure(self.start_direct('nobody-by-that-name'))

        self.assertEqual(blocked['message'], missing['message'])
        self.assertNotIn('block', blocked['message'].lower())

    def test_the_blocker_sees_the_flag_on_a_shared_member_list(self):
        # What lets a client render "Blocked" beside the name, and the Unblock
        # action on the profile behind it.
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        self.block(self.alice, 'bob')

        listed = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-participants', args=[group.id])
            )
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        by_name = {
            (r['user'] or {}).get('username'): r['user'] for r in rows if r['user']
        }
        self.assertTrue(by_name['bob']['blocked_by_me'])
        self.assertFalse(by_name['alice']['blocked_by_me'])

    def test_and_the_blocked_person_sees_no_such_flag(self):
        # It is never true because somebody blocked YOU - that would announce
        # the block to the person it was made against.
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        self.block(self.alice, 'bob')
        self.client.force_authenticate(self.bob)

        listed = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-participants', args=[group.id])
            )
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        by_name = {
            (r['user'] or {}).get('username'): r['user'] for r in rows if r['user']
        }
        self.assertFalse(by_name['alice']['blocked_by_me'])

    def test_an_unrelated_person_is_unaffected(self):
        make_user('carol')
        self.block(self.alice, 'bob')

        self.assertSuccess(self.start_direct('carol'))

    # --- an existing one goes read-only -------------------------------------

    def test_an_existing_direct_conversation_stops_taking_messages(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        create_message(conversation, self.alice, text='before')

        self.block(self.alice, 'bob')

        with self.assertRaises(chat_models.PostingNotAllowed):
            create_message(conversation, self.alice, text='after')

    def test_from_either_side(self):
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.block(self.alice, 'bob')

        with self.assertRaises(chat_models.PostingNotAllowed):
            create_message(conversation, self.bob, text='after')

    def test_but_the_history_is_still_there(self):
        # Read-only, not gone. Deleting somebody's own record of a conversation
        # they had is more than either person asked for.
        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        create_message(conversation, self.alice, text='before')

        self.block(self.alice, 'bob')

        self.assertEqual(conversation.messages.count(), 1)
        listed = self.assertSuccess(
            self.client.get(reverse('chat:conversation-list'))
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        self.assertIn(conversation.pk, [row['id'] for row in rows])

    def test_unblocking_makes_it_writable_again(self):
        from user import blocks

        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        self.block(self.alice, 'bob')
        blocks.unblock(self.alice, user=self.bob)

        message = create_message(conversation, self.alice, text='after')
        self.assertIsNotNone(message.pk)

    # --- invitations --------------------------------------------------------

    def test_a_blocked_person_cannot_add_me_to_a_group(self):
        self.block(self.alice, 'bob')
        group = Conversation.start_group(self.bob, name='Bobs Group')

        with self.assertRaises(membership.JoinRefused):
            membership.invite_by_email(group, 'alice@example.com', self.bob)

    def test_nor_i_them(self):
        self.block(self.alice, 'bob')
        group = Conversation.start_group(self.alice, name='Alices Group')

        with self.assertRaises(membership.JoinRefused):
            membership.invite_by_email(group, 'bob@example.com', self.alice)

    def test_a_blocked_number_cannot_be_invited(self):
        from user import blocks

        blocks.block(self.alice, phone_number='+639171234567')
        group = Conversation.start_group(self.alice, name='Family')

        with self.assertRaises(membership.JoinRefused):
            membership.invite_by_phone(
                group, '+639171234567', 'Mom', self.alice
            )

    def test_an_unverified_address_is_not_a_way_round_the_invite_refusal(self):
        # The bypass this nearly shipped with. A block is keyed on an account
        # or a number, never a bare address - and an UNVERIFIED address
        # resolves to nobody on purpose, so `invite_by_email` cannot match a
        # block and creates an unclaimed row instead. The refusal has to happen
        # at the moment that row would become real.
        carol = make_user('carol')
        self.block(self.alice, 'carol')
        group = Conversation.start_group(self.alice, name='Family')

        # Alice invites Carol by an address Carol has never verified.
        membership.invite_by_email(group, 'carol@example.com', self.alice)
        self.assertTrue(
            Participant.objects.filter(
                conversation=group, email='carol@example.com'
            ).exists()
        )

        # Carol verifies it. The invitation must not turn into membership.
        carol.profile.email_verified = True
        carol.profile.save(update_fields=['email_verified'])

        self.assertEqual(membership.claim_email_invitations(carol), 0)
        self.assertFalse(
            Participant.objects.filter(
                conversation=group, user=carol,
                status=Participant.Status.ACTIVE,
            ).exists()
        )

    def test_but_an_ordinary_invitation_still_claims(self):
        # The guard must not swallow the case the claim path exists for.
        carol = make_user('carol')
        group = Conversation.start_group(self.alice, name='Family')
        membership.invite_by_email(group, 'carol@example.com', self.alice)

        carol.profile.email_verified = True
        carol.profile.save(update_fields=['email_verified'])

        self.assertEqual(membership.claim_email_invitations(carol), 1)

    def test_adding_me_to_a_group_by_username_is_refused_too(self):
        # The route the app actually uses, and the one that was open: the
        # email and bare-number invites checked the block while
        # POST /conversations/<id>/members/ went straight to add_participant,
        # which knows only about the admin-imposed block.
        self.block(self.alice, 'bob')
        group = Conversation.start_group(self.bob, name='Bobs Group')
        self.client.force_authenticate(self.bob)

        response = self.client.post(
            reverse('chat:conversation-members', args=[group.id]),
            {'members': ['alice']},
            format='json',
        )

        self.assertFailure(response)
        self.assertFalse(
            Participant.objects.filter(
                conversation=group, user=self.alice,
                status=Participant.Status.ACTIVE,
            ).exists()
        )

    def test_and_adding_somebody_unrelated_still_works(self):
        make_user('carol')
        self.block(self.alice, 'bob')
        group = Conversation.start_group(self.bob, name='Bobs Group')
        self.client.force_authenticate(self.bob)

        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-members', args=[group.id]),
                {'members': ['carol']},
                format='json',
            )
        )

    def test_inviting_a_blocked_accounts_own_number_is_refused(self):
        # One person, two identities. A block recorded against the account had
        # to also stop an invitation addressed to that account's verified
        # number, or naming the other identity was a way straight back in.
        self.alice.profile.phone_number = '+639171230001'
        self.alice.profile.phone_no_verified = True
        self.alice.profile.save()
        self.block(self.alice, 'bob')
        group = Conversation.start_group(self.bob, name='Bobs Group')

        with self.assertRaises(membership.JoinRefused):
            membership.invite_by_phone(
                group, '+639171230001', 'A', self.bob
            )

    def test_but_an_unverified_number_is_not_taken_as_proof(self):
        # The mirror of the rule everywhere else: an unverified number is a
        # string somebody typed into their own profile, so honouring it would
        # let a squatter make themselves unaddressable with a stranger's.
        carol = make_user('carol')
        carol.profile.phone_number = '+639171230002'
        carol.profile.save()
        from user import blocks

        blocks.block(self.alice, user=carol)
        group = Conversation.start_group(self.alice, name='Family')

        row = membership.invite_by_phone(
            group, '+639171230002', 'C', self.alice
        )
        self.assertIsNotNone(row.pk)

    def test_a_blocked_direct_thread_does_not_leak_typing(self):
        # The one signal that would still have crossed a read-only thread:
        # each would watch the other start and stop typing in a conversation
        # neither can send into.
        from asgiref.sync import async_to_sync
        from chat.consumers import ChatConsumer

        conversation, _ = Conversation.start_direct(self.alice, self.bob)
        consumer = ChatConsumer()
        consumer.user = self.alice

        self.assertFalse(
            async_to_sync(consumer.direct_contact_is_blocked)(conversation)
        )

        self.block(self.alice, 'bob')

        self.assertTrue(
            async_to_sync(consumer.direct_contact_is_blocked)(conversation)
        )

    def test_and_a_group_never_suppresses_typing(self):
        from asgiref.sync import async_to_sync
        from chat.consumers import ChatConsumer

        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        self.block(self.alice, 'bob')

        consumer = ChatConsumer()
        consumer.user = self.alice
        self.assertFalse(
            async_to_sync(consumer.direct_contact_is_blocked)(group)
        )

    def test_an_unverified_number_is_not_a_way_round_the_invite_refusal(self):
        # The phone twin of the email hole. An invitation addressed to an
        # UNVERIFIED number cannot be checked against a block when it is sent,
        # because an unverified number resolves to nobody on purpose. So the
        # refusal has to happen when the row would become real.
        carol = make_user('carol')
        self.block(self.alice, 'carol')
        group = Conversation.start_group(self.alice, name='Family')

        # Alice invites a number Carol has not verified yet.
        membership.invite_by_phone(group, '+639171230009', 'C', self.alice)
        self.assertTrue(
            Participant.objects.filter(
                conversation=group, phone_number='+639171230009'
            ).exists()
        )

        # Carol verifies it and the row would fold into her account.
        carol.profile.phone_number = '+639171230009'
        carol.profile.phone_no_verified = True
        carol.profile.save()

        self.assertIsNone(group._claim_phone_participant(carol))
        self.assertFalse(
            Participant.objects.filter(
                conversation=group, user=carol,
                status=Participant.Status.ACTIVE,
            ).exists()
        )

    def test_but_an_ordinary_phone_invitation_still_claims(self):
        carol = make_user('carol')
        group = Conversation.start_group(self.alice, name='Family')
        membership.invite_by_phone(group, '+639171230010', 'C', self.alice)

        carol.profile.phone_number = '+639171230010'
        carol.profile.phone_no_verified = True
        carol.profile.save()

        claimed = group._claim_phone_participant(carol)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.user_id, carol.pk)

    # --- and the group is untouched -----------------------------------------

    def test_a_shared_group_is_not_changed_at_all(self):
        # Both stay members and both still see each other. A block suppresses
        # the notification, never the membership - that distinction is the
        # whole shape of the feature.
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)

        self.block(self.alice, 'bob')

        rows = Participant.objects.filter(conversation=group)
        self.assertEqual(rows.count(), 2)
        self.assertTrue(
            all(r.status == Participant.Status.ACTIVE for r in rows)
        )

    def test_and_they_can_still_post_there(self):
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        self.block(self.alice, 'bob')

        message = create_message(group, self.bob, text='Dinner at 7')

        self.assertIsNotNone(message.pk)
        self.assertEqual(group.messages.count(), 1)

    def test_the_ladder_itself_still_says_yes(self):
        # The membership half is untouched, and has to stay untouched: a block
        # is not a membership change. The pair question is asked NEXT to this
        # ladder, not inside it - which is why this still returns None.
        from chat import delivery

        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        alice_row = Participant.objects.get(conversation=group, user=self.alice)
        self.block(self.alice, 'bob')

        self.assertIsNone(
            delivery.refusal_reason(alice_row, ChannelConsent.Channel.SMS)
        )

    def test_but_the_audience_does_not(self):
        # The half that used to be missing. Bob posts; Alice blocked him; the
        # message is stored and visible, and only the notification stops.
        from chat import delivery

        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        alice_row = Participant.objects.get(conversation=group, user=self.alice)
        self.block(self.alice, 'bob')

        message = create_message(group, self.bob, text='Dinner at 7')
        send_to, skipped = delivery.eligible_for(
            message, ChannelConsent.Channel.SMS
        )

        self.assertEqual(send_to, [])
        self.assertEqual(
            dict((row.pk, why) for row, why in skipped)[alice_row.pk],
            delivery.BLOCKED_REASON,
        )

    def test_and_they_still_see_each_other_in_the_member_list(self):
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        self.block(self.alice, 'bob')

        listed = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-participants', args=[group.id])
            )
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        usernames = [
            (r['user'] or {}).get('username') for r in rows
        ]
        self.assertIn('bob', usernames)


class BlockedGroupDeliveryTests(EnvelopeAssertions, APITestCase):
    """No notification caused by somebody you blocked, inside a shared group.

    §17 gate 4 and §19 step 4 of the blocking spec. The message itself is
    untouched - it is stored, it is broadcast, and both people still read it in
    the app. What stops is the text, the email, and later the push and the
    voice alert.

    This is the half seven of the spec's eleven acceptance rows reduce to, so
    the rules it holds are the rules mentions, replies, reactions and push will
    inherit rather than each re-implement.
    """

    def setUp(self):
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.carol = make_user('carol', '+639170000003')
        self.group = Conversation.start_group(self.alice, name='Family')
        for account in (self.bob, self.carol):
            self.group.add_participant(account)
        self.rows = {
            account.username: Participant.objects.get(
                conversation=self.group, user=account
            )
            for account in (self.alice, self.bob, self.carol)
        }

    def block(self, blocker, blocked):
        from user import blocks

        blocks.block(blocker, user=blocked)

    def audience(self, sender, channel=ChannelConsent.Channel.SMS):
        """Who would be notified about a message from `sender`, and why not."""
        from chat import delivery

        message = create_message(self.group, sender, text='Dinner at 7')
        send_to, skipped = delivery.eligible_for(message, channel)
        return (
            sorted(row.user.username for row in send_to),
            dict((row.user.username, why) for row, why in skipped if row.user_id),
        )

    # --- the rule -----------------------------------------------------------

    def test_a_blocked_senders_message_does_not_notify_the_blocker(self):
        from chat import delivery

        self.block(self.alice, self.bob)

        send_to, skipped = self.audience(self.bob)

        self.assertNotIn('alice', send_to)
        self.assertEqual(skipped['alice'], delivery.BLOCKED_REASON)

    def test_and_blocking_one_person_does_not_silence_the_group(self):
        # §14. A blocked member must not turn off notifications from everybody
        # else - which is the failure a whole-conversation suppression would
        # produce, and nothing pinned it before.
        self.block(self.alice, self.bob)

        send_to, _ = self.audience(self.carol)

        self.assertEqual(send_to, ['alice', 'bob'])

    def test_it_holds_whichever_side_did_the_blocking(self):
        # §16. One-way would be detectable: Bob would notice his own messages
        # behaving differently from everybody else's.
        self.block(self.bob, self.alice)

        send_to, _ = self.audience(self.bob)

        self.assertNotIn('alice', send_to)

    def test_the_blocked_person_is_not_notified_either(self):
        self.block(self.alice, self.bob)

        send_to, _ = self.audience(self.alice)

        self.assertNotIn('bob', send_to)
        self.assertIn('carol', send_to)

    def test_everybody_else_is_untouched(self):
        self.block(self.alice, self.bob)

        send_to, _ = self.audience(self.bob)

        self.assertEqual(send_to, ['carol'])

    def test_it_applies_to_email_as_well_as_sms(self):
        # Both channels ask the same door, so neither needed its own change.
        for account in (self.alice, self.bob, self.carol):
            account.profile.email_verified = True
            account.profile.save(update_fields=['email_verified'])
        self.block(self.alice, self.bob)

        send_to, _ = self.audience(self.bob, ChannelConsent.Channel.EMAIL)

        self.assertEqual(send_to, ['carol'])

    # --- a sender with no account, which is §6's own example ----------------

    def test_a_bare_number_can_be_the_sender(self):
        # `Message.sender` is NULL when somebody with no account texts in, so
        # a block keyed on an account cannot name them. This is the scenario
        # §6 uses to illustrate the feature, and the spec's own pseudocode
        # quietly does nothing for it.
        from chat import delivery
        from user import blocks

        mom = self.group.add_phone_participant('+639171230077', 'Mom')
        membership.set_channel_consent(
            mom, ChannelConsent.Channel.SMS, ChannelConsent.Status.OPTED_IN
        )
        blocks.block(self.alice, phone_number='+639171230077')

        message = create_message(
            self.group, None, text='Dinner at 7', participant=mom
        )
        send_to, skipped = delivery.eligible_for(
            message, ChannelConsent.Channel.SMS
        )

        by_name = dict(
            (row.user.username, why) for row, why in skipped if row.user_id
        )
        self.assertEqual(by_name['alice'], delivery.BLOCKED_REASON)
        self.assertIn('bob', [row.user.username for row in send_to if row.user_id])

    # --- and the message itself is untouched --------------------------------

    def test_the_message_is_still_stored_and_still_readable(self):
        self.block(self.alice, self.bob)

        create_message(self.group, self.bob, text='Dinner at 7')

        self.assertEqual(self.group.messages.count(), 1)
        self.client.force_authenticate(self.alice)
        listed = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-messages', args=[self.group.id])
            )
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        self.assertEqual([row['text'] for row in rows], ['Dinner at 7'])

    def test_and_both_are_still_members(self):
        self.block(self.alice, self.bob)

        self.assertEqual(
            Participant.objects.filter(
                conversation=self.group, status=Participant.Status.ACTIVE
            ).count(),
            3,
        )

    # --- the cost -----------------------------------------------------------

    def test_the_screen_is_one_query_however_big_the_group(self):
        # Asked per recipient this is one round trip each - two hundred for a
        # two-hundred-member group, and two hundred again when email fans out
        # the same message. There is no other query-count guard in the suite.
        from chat import delivery

        for n in range(7):
            self.group.add_participant(make_user(f'extra{n}'))
        self.block(self.alice, self.bob)
        message = create_message(self.group, self.bob, text='Dinner at 7')

        # Two for the audience (participants + the consent prefetch), one for
        # the screen. Not one per member.
        with self.assertNumQueries(3):
            delivery.eligible_for(message, ChannelConsent.Channel.SMS)

class ReplyToMessageTests(EnvelopeAssertions, APITestCase):
    """Answering one message with another.

    A reply is an ordinary message that happens to name a parent, so it needs
    no new event and no new table - just a nullable self-FK and one guard.

    That guard is the whole security story: without it the id of a message in a
    group you are not in can be passed as a parent, and its text comes straight
    back inside the reply excerpt. "Send a message" would become a way to read
    private conversations.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.parent = create_message(
            self.group, self.alice, text='why is my name not showing?'
        )
        self.client.force_authenticate(self.bob)
        self.url = reverse('chat:conversation-messages', args=[self.group.pk])

    def send(self, text, reply_to=None):
        payload = {'text': text}
        if reply_to is not None:
            payload['reply_to'] = reply_to
        return self.client.post(self.url, payload, format='json')

    # --- the happy path -------------------------------------------------

    def test_a_reply_names_its_parent(self):
        data = self.assertSuccess(
            self.send('yes it is', reply_to=self.parent.pk),
            status.HTTP_201_CREATED,
        )

        self.assertEqual(data['reply_to']['id'], self.parent.pk)
        self.assertEqual(data['reply_to']['sender'], 'alice')
        self.assertEqual(
            data['reply_to']['text'], 'why is my name not showing?'
        )
        self.assertEqual(
            Message.objects.get(pk=data['id']).reply_to_id, self.parent.pk
        )

    def test_an_ordinary_message_replies_to_nothing(self):
        data = self.assertSuccess(
            self.send('just talking'), status.HTTP_201_CREATED
        )
        self.assertIsNone(data['reply_to'])

    def test_a_long_parent_is_excerpted(self):
        long_parent = create_message(self.group, self.alice, text='x' * 400)
        data = self.assertSuccess(
            self.send('short answer', reply_to=long_parent.pk),
            status.HTTP_201_CREATED,
        )

        excerpt = data['reply_to']['text']
        self.assertLess(len(excerpt), 400)
        self.assertTrue(excerpt.endswith('\u2026'))

    # --- the guard ------------------------------------------------------

    def test_a_parent_from_another_conversation_is_refused(self):
        """The one that matters. Otherwise this endpoint reads private groups."""
        elsewhere = Conversation.start_group(self.alice, name='Secret')
        secret = create_message(
            elsewhere, self.alice, text='the door code is 1234'
        )

        response = self.send('nice try', reply_to=secret.pk)

        self.assertFailure(response)
        self.assertFalse(
            Message.objects.filter(conversation=self.group, text='nice try')
            .exists()
        )

    def test_a_parent_that_does_not_exist_is_refused_the_same_way(self):
        # Word for word the same refusal as the cross-conversation case: a
        # distinguishable "no such message" would confirm which ids are real.
        missing = self.send('to nothing', reply_to=999999)
        other = self.send(
            'to elsewhere',
            reply_to=create_message(
                Conversation.start_group(self.alice, name='Other'),
                self.alice,
                text='hi',
            ).pk,
        )

        self.assertEqual(self.assertFailure(missing), self.assertFailure(other))

    # --- the parent going away -------------------------------------------

    def test_a_reply_outlives_its_parent(self):
        """SET_NULL, not CASCADE. Deleting a message must not delete answers."""
        reply = create_message(
            self.group, self.bob, text='yes it is', reply_to=self.parent
        )

        self.parent.delete()

        reply.refresh_from_db()
        self.assertIsNone(reply.reply_to_id)
        self.assertTrue(Message.objects.filter(pk=reply.pk).exists())

    def test_the_excerpt_is_dropped_when_the_parent_is_gone(self):
        reply = create_message(
            self.group, self.bob, text='yes it is', reply_to=self.parent
        )
        self.parent.delete()
        reply.refresh_from_db()

        self.assertIsNone(MessageSerializer(reply).data['reply_to'])

    def test_a_phone_only_parent_is_credited_by_its_membership(self):
        # sender_label falls back to the participant row, so a reply to an
        # inbound SMS still names somebody.
        mom = self.group.add_phone_participant('+639171111111', display_name='Mom')
        texted = create_message(
            self.group,
            None,
            text='dinner at 7',
            participant=mom,
            source=Message.Source.SMS,
        )

        data = self.assertSuccess(
            self.send('ok', reply_to=texted.pk), status.HTTP_201_CREATED
        )
        self.assertEqual(data['reply_to']['sender'], 'Mom')



THUMBS_UP, HEART = REACTION_EMOJI[0], REACTION_EMOJI[1]


class MessageActionRouteMixin:
    """URL helpers for the per-message routes.

    They hang off the conversation rather than off a top-level /messages/<id>/,
    so every one of them carries the conversation id - and that is what proves
    membership before the message is even looked up.
    """

    def message_url(self, message, suffix=''):
        return (
            reverse('chat:conversation-detail', args=[message.conversation_id])
            + f'messages/{message.pk}/'
            + suffix
        )


class ReactToMessageTests(
    MessageActionRouteMixin, EnvelopeAssertions, APITestCase
):
    """A thumbs up, and everything it must NOT do.

    The load-bearing rule is that a reaction is app-only: it never goes through
    create_message, so it cannot fan out to a phone. A thumbs up that billed an
    SMS segment to every member of a two-hundred-person group would be an
    expensive way to say nothing.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.message = create_message(self.group, self.alice, text='dinner at 7')
        self.client.force_authenticate(self.bob)
        self.url = self.message_url(self.message, 'reactions/')

    def react(self, emoji=THUMBS_UP):
        return self.client.post(self.url, {'emoji': emoji}, format='json')

    def unreact(self, emoji=THUMBS_UP):
        return self.client.delete(f'{self.url}?emoji={emoji}')

    # --- the happy path ---------------------------------------------------

    def test_a_reaction_comes_back_grouped_with_who_left_it(self):
        data = self.assertSuccess(self.react())

        self.assertEqual(
            data['reactions'],
            [{'emoji': THUMBS_UP, 'count': 1, 'usernames': ['bob']}],
        )

    def test_usernames_are_what_tells_a_client_it_already_reacted(self):
        # Not a `reacted` boolean: this same payload is broadcast, and a
        # broadcast has no request to compute "me" against.
        self.react()
        self.client.force_authenticate(self.alice)
        data = self.assertSuccess(self.react())

        self.assertEqual(data['reactions'][0]['count'], 2)
        self.assertCountEqual(
            data['reactions'][0]['usernames'], ['alice', 'bob']
        )

    def test_different_emoji_are_separate_pills(self):
        self.react(THUMBS_UP)
        data = self.assertSuccess(self.react(HEART))

        self.assertEqual([r['emoji'] for r in data['reactions']],
                         [THUMBS_UP, HEART])

    def test_taking_it_back_removes_it(self):
        self.react()
        data = self.assertSuccess(self.unreact())

        self.assertEqual(data['reactions'], [])
        self.assertFalse(MessageReaction.objects.exists())

    # --- idempotence ------------------------------------------------------

    def test_reacting_twice_leaves_one_reaction(self):
        self.react()
        data = self.assertSuccess(self.react())

        self.assertEqual(data['reactions'][0]['count'], 1)
        self.assertEqual(MessageReaction.objects.count(), 1)

    def test_taking_back_a_reaction_that_is_not_there_is_not_an_error(self):
        # A retry after a dropped response must not be an error the UI has to
        # explain, and must not flip the state back.
        self.assertSuccess(self.unreact())

    # --- the rule that costs money ---------------------------------------

    def test_a_reaction_never_fans_out(self):
        with mock.patch('chat.services._schedule_delivery') as fan_out:
            self.react()

        fan_out.assert_not_called()

    def test_a_reaction_is_not_a_message(self):
        before = Message.objects.count()
        self.react()

        self.assertEqual(Message.objects.count(), before)

    # --- what may be reacted to ------------------------------------------

    def test_only_the_listed_emoji_are_accepted(self):
        # Free text here would let any member put 32 arbitrary characters
        # under a message for the whole group to look at.
        self.assertFailure(
            self.client.post(self.url, {'emoji': 'not an emoji'}, format='json')
        )

    def test_a_message_in_another_group_cannot_be_reacted_to(self):
        elsewhere = Conversation.start_group(self.alice, name='Secret')
        secret = create_message(elsewhere, self.alice, text='the address is')

        response = self.client.post(
            self.message_url(secret, 'reactions/'),
            {'emoji': THUMBS_UP},
            format='json',
        )

        self.assertFailure(response, status.HTTP_404_NOT_FOUND)

    def test_a_deleted_message_cannot_be_reacted_to(self):
        self.message.deleted_at = timezone.now()
        self.message.save(update_fields=['deleted_at'])

        self.assertFailure(self.react())


class EditMessageTests(MessageActionRouteMixin, EnvelopeAssertions, APITestCase):
    """Fixing your own words.

    In-app only. The copy already texted or emailed out cannot be recalled, so
    this changes what the app shows and makes no claim about the handset that
    already has the original.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.message = create_message(self.group, self.bob, text='dinner at 6')
        self.client.force_authenticate(self.bob)
        self.url = self.message_url(self.message)

    def edit(self, text='dinner at 7'):
        return self.client.patch(self.url, {'text': text}, format='json')

    def test_an_edit_replaces_the_words_and_says_it_was_edited(self):
        data = self.assertSuccess(self.edit())

        self.assertEqual(data['text'], 'dinner at 7')
        self.assertIsNotNone(data['edited_at'])

    def test_an_unedited_message_says_so(self):
        data = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-messages', args=[self.group.pk])
            )
        )

        self.assertIsNone(data['results'][0]['edited_at'])

    def test_nobody_edits_somebody_elses_words(self):
        # Not even an admin: rewriting a message under its author's name would
        # put words in their mouth. An admin who objects can delete it, which
        # is visible for what it is.
        self.client.force_authenticate(self.alice)

        self.assertFailure(self.edit(), status.HTTP_403_FORBIDDEN)

    def test_an_empty_edit_is_refused(self):
        self.assertFailure(self.edit('   '))

    def test_a_texted_in_message_cannot_be_edited(self):
        # It is a copy of something that really was sent from a handset;
        # editing the copy would make the thread disagree with what arrived.
        texted = create_message(
            self.group, None, text='on my way', source=Message.Source.SMS
        )
        texted.sender = self.bob
        texted.save(update_fields=['sender'])

        self.assertFailure(self.client.patch(
            self.message_url(texted), {'text': 'nope'}, format='json'
        ))


class DeleteMessageTests(
    MessageActionRouteMixin, EnvelopeAssertions, APITestCase
):
    """Taking a message out of the thread, for everyone.

    SOFT, always. The row survives so replies quoting it keep a parent and so
    read pointers do not dangle; the serializer is what withholds the content.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.message = create_message(self.group, self.bob, text='ignore this')
        self.client.force_authenticate(self.bob)

    def delete(self, message=None):
        return self.client.delete(self.message_url(message or self.message))

    def test_the_row_survives_but_the_words_do_not(self):
        data = self.assertSuccess(self.delete())

        self.assertEqual(data['text'], '')
        self.assertIsNotNone(data['deleted_at'])
        self.assertTrue(Message.objects.filter(pk=self.message.pk).exists())

    def test_an_admin_may_delete_anybody_s_message(self):
        self.client.force_authenticate(self.alice)

        self.assertSuccess(self.delete())

    def test_an_ordinary_member_may_not_delete_somebody_else_s(self):
        hers = create_message(self.group, self.alice, text='mine')

        self.assertFailure(self.delete(hers), status.HTTP_403_FORBIDDEN)

    def test_deleting_twice_is_not_an_error(self):
        self.delete()

        self.assertSuccess(self.delete())

    def test_attachments_go_with_it(self):
        Attachment.objects.create(
            message=self.message,
            file=SimpleUploadedFile('secret.txt', b'x'),
            original_name='secret.txt',
        )

        self.assertEqual(self.assertSuccess(self.delete())['attachments'], [])

    def test_a_reply_keeps_its_place_but_not_the_quote(self):
        reply = create_message(
            self.group, self.alice, text='why?', reply_to=self.message
        )
        self.delete()

        quoted = MessageSerializer(Message.objects.get(pk=reply.pk)).data
        self.assertEqual(quoted['reply_to']['text'], 'This message was deleted')

    def test_deleting_the_pinned_message_clears_the_pin(self):
        self.client.force_authenticate(self.alice)
        self.client.post(self.message_url(self.message, 'pin/'))

        self.delete()

        self.group.refresh_from_db()
        self.assertIsNone(self.group.pinned_message_id)


class PinMessageTests(MessageActionRouteMixin, EnvelopeAssertions, APITestCase):
    """Holding one message above the thread.

    One pin, stored as a column on the conversation, so the change rides the
    `conversation.updated` broadcast every client already handles rather than
    needing a socket event of its own.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.message = create_message(self.group, self.alice, text='the code is 4821')
        self.client.force_authenticate(self.alice)
        self.url = self.message_url(self.message, 'pin/')

    def test_pinning_puts_it_on_the_conversation(self):
        data = self.assertSuccess(self.client.post(self.url))

        self.assertEqual(data['pinned_message']['id'], self.message.pk)

    def test_pinning_a_second_message_replaces_the_first(self):
        self.client.post(self.url)
        newer = create_message(self.group, self.alice, text='no, 1234')

        data = self.assertSuccess(
            self.client.post(self.message_url(newer, 'pin/'))
        )

        self.assertEqual(data['pinned_message']['id'], newer.pk)

    def test_unpinning_clears_it(self):
        self.client.post(self.url)

        data = self.assertSuccess(self.client.delete(self.url))

        self.assertIsNone(data['pinned_message'])

    def test_unpinning_a_message_that_is_not_the_pinned_one_leaves_it_alone(self):
        # Two admins unpinning at once must not have the second clear a pin the
        # first already replaced.
        self.client.post(self.url)
        other = create_message(self.group, self.alice, text='unrelated')

        self.client.delete(self.message_url(other, 'pin/'))

        self.group.refresh_from_db()
        self.assertEqual(self.group.pinned_message_id, self.message.pk)

    def test_an_ordinary_member_cannot_pin(self):
        self.client.force_authenticate(self.bob)

        self.assertFailure(
            self.client.post(self.url), status.HTTP_403_FORBIDDEN
        )

    def test_either_person_can_pin_in_a_one_to_one(self):
        # There are no admins in a direct chat, so an admins-only rule would
        # make the feature unreachable in half the app.
        direct, _ = Conversation.start_direct(self.alice, self.bob)
        message = create_message(direct, self.alice, text='my address')
        self.client.force_authenticate(self.bob)

        self.assertSuccess(self.client.post(self.message_url(message, 'pin/')))


class ForwardMessageTests(
    MessageActionRouteMixin, EnvelopeAssertions, APITestCase
):
    """Passing a message along to another conversation.

    Posted to the DESTINATION, so every check that guards a normal send guards
    a forward too. The words come from the original, never from the request.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.source = Conversation.start_group(self.alice, name='Work')
        self.source.add_participant(self.bob)
        self.target = Conversation.start_group(self.bob, name='Family')
        self.original = create_message(
            self.source, self.alice, text='the venue moved to Pier 4'
        )
        self.client.force_authenticate(self.bob)
        self.url = reverse('chat:conversation-messages', args=[self.target.pk])

    def forward(self, message_id=None, **extra):
        return self.client.post(
            self.url,
            {'forward_from': message_id or self.original.pk, **extra},
            format='json',
        )

    def test_a_forward_carries_the_original_words(self):
        data = self.assertSuccess(self.forward(), status.HTTP_201_CREATED)

        self.assertEqual(data['text'], 'the venue moved to Pier 4')
        self.assertTrue(data['forwarded'])

    def test_a_forward_never_names_who_it_came_from(self):
        # It crosses conversations, so naming the original sender would tell
        # the new room about somebody in a room they cannot see.
        data = self.assertSuccess(self.forward(), status.HTTP_201_CREATED)

        self.assertNotIn('forwarded_from', data)
        self.assertEqual(data['sender']['username'], 'bob')

    def test_the_forwarder_cannot_supply_their_own_words(self):
        # Otherwise anybody could put words in a "Forwarded" frame that were
        # never said.
        data = self.assertSuccess(
            self.forward(text='the venue moved to my house'),
            status.HTTP_201_CREATED,
        )

        self.assertEqual(data['text'], 'the venue moved to Pier 4')

    def test_files_come_along_without_being_stored_twice(self):
        upload = SimpleUploadedFile('map.txt', b'pier 4')
        attachment = Attachment.objects.create(
            message=self.original, file=upload, original_name='map.txt'
        )

        data = self.assertSuccess(self.forward(), status.HTTP_201_CREATED)

        self.assertEqual(data['attachments'][0]['original_name'], 'map.txt')
        copied = Attachment.objects.get(pk=data['attachments'][0]['id'])
        self.assertEqual(copied.file.name, attachment.file.name)

    def test_a_message_from_a_conversation_you_are_not_in_cannot_be_forwarded(self):
        # The read-across this endpoint would otherwise be: unchecked, it
        # copies the text of any message in the database into a conversation
        # the caller controls.
        stranger = make_user('carol', '+639170000003')
        theirs = Conversation.start_group(stranger, name='Private')
        secret = create_message(theirs, stranger, text='the password is hunter2')

        self.assertFailure(self.forward(secret.pk))

    def test_a_message_from_a_group_you_left_cannot_be_forwarded(self):
        self.source.participants.filter(user=self.bob).update(
            status=Participant.Status.LEFT
        )

        self.assertFailure(self.forward())

    def test_a_deleted_message_cannot_be_forwarded(self):
        self.original.deleted_at = timezone.now()
        self.original.save(update_fields=['deleted_at'])

        self.assertFailure(self.forward())

    def test_a_forward_is_delivered_like_any_other_message(self):
        # It is a real message in the destination, so the people there who take
        # this group by text get it by text. Anything else would silently drop
        # it for the members who are not in the app.
        with mock.patch('chat.services._schedule_delivery') as fan_out:
            self.forward()

        fan_out.assert_called_once()


class _FakePage:
    """Just enough of a `requests` response for chat/link_previews.py."""

    def __init__(self, status=200, headers=None, body=b''):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def iter_content(self, size):
        for index in range(0, len(self._body), size):
            yield self._body[index:index + size]

    def close(self):
        pass


def _resolves_to(*addresses):
    import socket

    return [
        (socket.AF_INET6 if ':' in a else socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443))
        for a in addresses
    ]


class LinkPreviewTests(APITestCase):
    """The card under a link: what is fetched, what is refused, whose setting counts."""

    PUBLIC = '93.184.216.34'
    HTML = {'Content-Type': 'text/html; charset=utf-8'}

    def setUp(self):
        self.alice = make_user('alice', '+639170000301')
        self.group = Conversation.start_group(self.alice, name='Links')

    def fetch(self, url, pages, addresses=None):
        from chat import link_previews

        with mock.patch('chat.link_previews.socket.getaddrinfo',
                        side_effect=addresses or (lambda *a, **k: _resolves_to(self.PUBLIC))), \
                mock.patch('chat.link_previews.requests.get', side_effect=pages) as get:
            return link_previews.fetch_preview(url), get

    # --- finding the link ---------------------------------------------------

    def test_the_first_link_is_used_without_trailing_punctuation(self):
        from chat.link_previews import first_link

        self.assertEqual(
            first_link('see https://example.com/a?b=1. and http://other.test'),
            'https://example.com/a?b=1',
        )
        self.assertIsNone(first_link('no link here, just www.example.com'))

    # --- reading a page ------------------------------------------------------

    def test_a_page_gives_its_open_graph_card(self):
        body = (b'<html><head><title>Fallback</title>'
                b'<meta property="og:title" content="A  Real   Title">'
                b'<meta property="og:description" content="What it is about">'
                b'<meta property="og:image" content="/cover.png">'
                b'<meta property="og:site_name" content="Example"></head><body></body></html>')

        card, _ = self.fetch('https://example.com/post', [_FakePage(headers=self.HTML, body=body)])

        self.assertEqual(card, {
            'url': 'https://example.com/post',
            'title': 'A Real Title',
            'description': 'What it is about',
            'image_url': 'https://example.com/cover.png',
            'site_name': 'Example',
        })

    def test_the_title_and_meta_description_are_the_fallback(self):
        body = b'<head><title>Plain page</title><meta name="description" content="Words"></head>'

        card, _ = self.fetch('https://example.com/', [_FakePage(headers=self.HTML, body=body)])

        self.assertEqual((card['title'], card['description'], card['image_url']),
                         ('Plain page', 'Words', None))
        self.assertEqual(card['site_name'], 'example.com')

    def test_an_http_picture_is_not_kept(self):
        # Mixed content on an https page, which browsers block anyway.
        body = b'<head><meta property="og:title" content="T"><meta property="og:image" content="http://example.com/p.png"></head>'

        card, _ = self.fetch('https://example.com/', [_FakePage(headers=self.HTML, body=body)])

        self.assertIsNone(card['image_url'])

    def test_a_file_that_is_neither_a_page_nor_an_image_has_no_card(self):
        card, _ = self.fetch('https://example.com/file.pdf',
                             [_FakePage(headers={'Content-Type': 'application/pdf'}, body=b'%PDF')])
        self.assertIsNone(card)

    def test_nothing_past_the_size_cap_is_read(self):
        from chat import link_previews

        body = b'<head>' + b' ' * 200 + b'<meta property="og:title" content="Too far"></head>'
        with mock.patch.object(link_previews, 'MAX_BYTES', 100):
            card, _ = self.fetch('https://example.com/', [_FakePage(headers=self.HTML, body=body)])
        self.assertIsNone(card)

    # --- what is refused -----------------------------------------------------

    def test_internal_addresses_are_refused_before_anything_is_fetched(self):
        from chat.link_previews import UnsafeLink

        for address in ('127.0.0.1', '10.0.0.5', '192.168.1.1', '169.254.169.254', '::1', '::ffff:127.0.0.1'):
            with self.subTest(address=address):
                with self.assertRaises(UnsafeLink):
                    self.fetch('https://example.com/', [], addresses=lambda *a, _x=address, **k: _resolves_to(_x))

    def test_a_redirect_to_an_internal_address_is_refused(self):
        from chat.link_previews import UnsafeLink

        answers = iter([_resolves_to(self.PUBLIC), _resolves_to('10.0.0.1')])
        with self.assertRaises(UnsafeLink):
            self.fetch(
                'https://example.com/',
                [_FakePage(status=302, headers={'Location': 'http://internal.test/admin'})],
                addresses=lambda *a, **k: next(answers),
            )

    def test_non_web_schemes_odd_ports_and_credentials_are_refused(self):
        from chat.link_previews import UnsafeLink

        for url in ('ftp://example.com/', 'https://example.com:8080/', 'https://user:pw@example.com/'):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeLink):
                    self.fetch(url, [])

    # --- whose setting, and what happens -----------------------------------

    def test_the_senders_setting_decides(self):
        from chat.link_previews import wants_preview
        from user.models import MessagingSettings

        message = Message.objects.create(
            conversation=self.group, sender=self.alice, text='https://example.com'
        )
        self.assertTrue(wants_preview(message))

        settings_row = MessagingSettings.for_user(self.alice)
        settings_row.link_previews_enabled = False
        settings_row.save(update_fields=['link_previews_enabled'])
        message.sender.refresh_from_db()
        self.assertFalse(wants_preview(Message.objects.get(pk=message.pk)))

    def test_no_card_for_a_message_without_an_account_or_a_link(self):
        from chat.link_previews import wants_preview

        phone_only = Message.objects.create(conversation=self.group, sender=None, text='https://example.com')
        no_link = Message.objects.create(conversation=self.group, sender=self.alice, text='hello')

        self.assertFalse(wants_preview(phone_only))
        self.assertFalse(wants_preview(no_link))

    def test_refreshing_saves_the_card_and_tells_everyone(self):
        from chat import link_previews

        message = Message.objects.create(conversation=self.group, sender=self.alice, text='https://example.com')
        card = {'url': 'https://example.com', 'title': 'T', 'description': None,
                'image_url': None, 'site_name': 'example.com'}

        with mock.patch.object(link_previews, 'fetch_preview', return_value=card), \
                mock.patch('chat.services.broadcast_message_updated') as broadcast:
            link_previews.refresh_preview(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.link_preview, card)
        broadcast.assert_called_once()

    def test_a_refused_link_leaves_no_card_and_sends_nothing(self):
        from chat import link_previews

        message = Message.objects.create(conversation=self.group, sender=self.alice, text='http://localhost/')
        with mock.patch.object(link_previews, 'fetch_preview', side_effect=link_previews.UnsafeLink('internal')), \
                mock.patch('chat.services.broadcast_message_updated') as broadcast:
            link_previews.refresh_preview(message.pk)

        message.refresh_from_db()
        self.assertIsNone(message.link_preview)
        broadcast.assert_not_called()

    def test_sending_a_message_schedules_its_card(self):
        with mock.patch('chat.link_previews.schedule_link_preview') as scheduled:
            create_message(self.group, self.alice, text='look https://example.com')
        scheduled.assert_called_once()

    def test_a_deleted_message_does_not_show_its_card(self):
        from django.utils import timezone
        from chat.serializers import MessageSerializer

        message = Message.objects.create(
            conversation=self.group, sender=self.alice, text='https://example.com',
            link_preview={'url': 'https://example.com', 'title': 'T'},
        )
        self.assertEqual(MessageSerializer(message).data['link_preview']['title'], 'T')

        message.deleted_at = timezone.now()
        message.save(update_fields=['deleted_at'])
        self.assertIsNone(MessageSerializer(message).data['link_preview'])


class YouTubeLinkPreviewTests(APITestCase):
    """YouTube's card comes from oEmbed: its page puts the tags past the read cap."""

    def fetch(self, url, pages):
        from chat import link_previews

        with mock.patch('chat.link_previews.socket.getaddrinfo',
                        side_effect=lambda *a, **k: _resolves_to('142.250.66.78')), \
                mock.patch('chat.link_previews.requests.get', side_effect=pages) as get:
            return link_previews.fetch_preview(url), get

    def answer(self, **data):
        import json

        return _FakePage(headers={'Content-Type': 'application/json'},
                         body=json.dumps(data).encode())

    def test_a_youtube_link_is_asked_of_oembed_not_read_from_the_page(self):
        link = 'https://www.youtube.com/watch?v=7h-ygnzJek0&list=RDMM7h-ygnzJek0&start_radio=1'
        card, get = self.fetch(link, [self.answer(
            title='Fallen (Live at The Cozy Cove) - Lola Amour',
            author_name='Nine Degrees North',
            provider_name='YouTube',
            thumbnail_url='https://i.ytimg.com/vi/7h-ygnzJek0/hqdefault.jpg',
        )])

        requested = get.call_args.args[0]
        self.assertTrue(requested.startswith('https://www.youtube.com/oembed?'))
        self.assertIn('url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D7h-ygnzJek0', requested)
        self.assertEqual(card, {
            'url': link,
            'title': 'Fallen (Live at The Cozy Cove) - Lola Amour',
            'description': 'Nine Degrees North',
            'image_url': 'https://i.ytimg.com/vi/7h-ygnzJek0/hqdefault.jpg',
            'site_name': 'YouTube',
        })

    def test_short_and_mobile_links_are_asked_too(self):
        for link in ('https://youtu.be/7h-ygnzJek0', 'https://m.youtube.com/watch?v=7h-ygnzJek0'):
            with self.subTest(link=link):
                card, get = self.fetch(link, [self.answer(title='T', provider_name='YouTube')])
                self.assertTrue(get.call_args.args[0].startswith('https://www.youtube.com/oembed?'))
                self.assertEqual(card['title'], 'T')

    def test_an_unavailable_video_has_no_card(self):
        card, _ = self.fetch('https://www.youtube.com/watch?v=gone', [_FakePage(status=404)])
        self.assertIsNone(card)

    def test_an_http_thumbnail_is_not_kept(self):
        card, _ = self.fetch('https://youtu.be/x', [self.answer(
            title='T', thumbnail_url='http://i.ytimg.com/vi/x/hqdefault.jpg')])
        self.assertIsNone(card['image_url'])

    def test_other_sites_are_still_read_from_the_page(self):
        body = b'<head><meta property="og:title" content="Page"></head>'
        card, get = self.fetch('https://example.com/post',
                               [_FakePage(headers={'Content-Type': 'text/html'}, body=body)])
        self.assertEqual(get.call_args.args[0], 'https://example.com/post')
        self.assertEqual(card['title'], 'Page')


class _NeverReadPage(_FakePage):
    """An image response whose body must not be downloaded."""

    def iter_content(self, size):
        raise AssertionError('the image body was read')


class OtherLinkPreviewTests(APITestCase):
    """Images, Google Images results and TikTok: links a page read cannot card."""

    def fetch(self, url, pages, address='142.250.66.78'):
        from chat import link_previews

        resolve = address if callable(address) else (lambda *a, **k: _resolves_to(address))
        with mock.patch('chat.link_previews.socket.getaddrinfo', side_effect=resolve), \
                mock.patch('chat.link_previews.requests.get', side_effect=pages) as get:
            return link_previews.fetch_preview(url), get

    def test_a_link_to_an_image_is_a_card_of_the_picture_without_downloading_it(self):
        link = 'https://i.ytimg.com/vi/7h-ygnzJek0/hqdefault.jpg'
        card, _ = self.fetch(link, [_NeverReadPage(headers={'Content-Type': 'image/jpeg'})])

        self.assertEqual(card, {'url': link, 'title': None, 'description': None,
                                'image_url': link, 'site_name': 'i.ytimg.com'})

    def test_an_http_image_link_has_no_card(self):
        card, _ = self.fetch('http://example.com/photo.jpg',
                             [_NeverReadPage(headers={'Content-Type': 'image/jpeg'})])
        self.assertIsNone(card)

    def test_a_google_images_result_is_a_card_of_the_picture_it_names(self):
        image = 'https://i.ytimg.com/vi/7h-ygnzJek0/hqdefault.jpg'
        link = ('https://www.google.com/imgres?imgurl=https%3A%2F%2Fi.ytimg.com%2Fvi%2F7h-ygnzJek0'
                '%2Fhqdefault.jpg&imgrefurl=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D7h-ygnzJek0')
        card, get = self.fetch(link, [_NeverReadPage(headers={'Content-Type': 'image/jpeg'})])

        # Google's page is never fetched - only the picture, to confirm it.
        self.assertEqual(get.call_args_list[0].args[0], image)
        self.assertEqual((card['image_url'], card['site_name'], card['url']),
                         (image, 'www.youtube.com', link))

    def test_a_google_images_result_on_a_country_domain_counts(self):
        link = 'https://www.google.com.ph/imgres?imgurl=https%3A%2F%2Fexample.com%2Fp.png'
        card, _ = self.fetch(link, [_NeverReadPage(headers={'Content-Type': 'image/png'})])
        self.assertEqual((card['image_url'], card['site_name']),
                         ('https://example.com/p.png', 'Google Images'))

    def test_a_google_images_result_that_is_not_really_an_image_has_no_card(self):
        link = 'https://www.google.com/imgres?imgurl=https%3A%2F%2Fexample.com%2Fnot-an-image'
        card, _ = self.fetch(link, [_FakePage(headers={'Content-Type': 'text/html'}, body=b'<html>')])
        self.assertIsNone(card)

    def test_a_google_images_result_naming_an_internal_address_is_refused(self):
        from chat.link_previews import UnsafeLink

        link = 'https://www.google.com/imgres?imgurl=http%3A%2F%2F169.254.169.254%2Flatest%2F'
        with self.assertRaises(UnsafeLink):
            self.fetch(link, [], address='169.254.169.254')

    def test_a_tiktok_link_is_asked_of_oembed(self):
        import json

        link = 'https://www.tiktok.com/@scout2015/video/6718335390845095173'
        answer = _FakePage(headers={'Content-Type': 'application/json'}, body=json.dumps({
            'title': 'A video', 'author_name': 'Scout', 'provider_name': 'TikTok',
            'thumbnail_url': 'https://p16.tiktokcdn.com/x.jpeg',
        }).encode())
        card, get = self.fetch(link, [answer])

        self.assertTrue(get.call_args.args[0].startswith('https://www.tiktok.com/oembed?'))
        self.assertEqual((card['title'], card['description'], card['site_name']),
                         ('A video', 'Scout', 'TikTok'))


def set_privacy(user, **answers):
    """One person's Settings → Privacy, as the API would have saved them."""
    from user.models import PrivacySettings

    row = PrivacySettings.for_user(user)
    for field, value in answers.items():
        setattr(row, field, value)
    row.save(update_fields=list(answers))
    return row


class DirectMessagePrivacyTests(EnvelopeAssertions, APITestCase):
    """"Who can send me direct messages", from Settings → Privacy.

    Only STARTING one is refused. A thread the two already have carries on:
    stranding a conversation somebody is in is what blocking is for.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')

    def start(self, starter, identifier='alice'):
        self.client.force_authenticate(starter)
        return self.client.post(
            reverse('chat:conversation-direct'), {'identifier': identifier},
            format='json',
        )

    def assertStarted(self, response):
        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_201_CREATED),
            response.json(),
        )
        self.assertEqual(response.json()['code'], 1)

    def audience(self, name):
        from user.models import PrivacySettings

        return getattr(PrivacySettings.Audience, name)

    def test_everyone_is_the_default(self):
        self.assertStarted(self.start(self.bob))

    def test_nobody_refuses_and_says_why(self):
        set_privacy(self.alice, direct_messages_from=self.audience('NOBODY'))

        body = self.assertFailure(self.start(self.bob))

        self.assertIn("doesn't accept direct messages", body['message'])
        self.assertEqual(
            Conversation.objects.filter(kind=Conversation.Kind.DIRECT).count(), 0
        )

    def test_loops_refuses_somebody_with_no_group_in_common(self):
        set_privacy(self.alice, direct_messages_from=self.audience('LOOPS'))

        self.assertFailure(self.start(self.bob))

    def test_loops_allows_somebody_from_a_shared_group(self):
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        set_privacy(self.alice, direct_messages_from=self.audience('LOOPS'))

        self.assertStarted(self.start(self.bob))

    def test_a_thread_they_already_have_still_opens(self):
        self.assertStarted(self.start(self.bob))
        set_privacy(self.alice, direct_messages_from=self.audience('NOBODY'))

        self.assertStarted(self.start(self.bob))

    def test_their_own_setting_never_refuses_themselves(self):
        # Alice writing to Bob is Bob's business, not Alice's own setting.
        set_privacy(self.alice, direct_messages_from=self.audience('NOBODY'))

        self.assertStarted(self.start(self.alice, identifier='bob'))


class GroupAddPrivacyTests(EnvelopeAssertions, APITestCase):
    """"Who can add me to groups", on every route that adds somebody."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.bob.profile.email_verified = True
        self.bob.profile.save(update_fields=['email_verified'])

    def audience(self, name):
        from user.models import PrivacySettings

        return getattr(PrivacySettings.Audience, name)

    def create_group(self, members=None, **extra):
        self.client.force_authenticate(self.alice)
        payload = {'name': 'Trip', **extra}
        if members:
            payload['members'] = members
        return self.client.post(
            reverse('chat:conversation-group'), payload, format='json'
        )

    def add_members(self, group, members):
        self.client.force_authenticate(self.alice)
        return self.client.post(
            reverse('chat:conversation-members', args=[group.id]),
            {'members': members}, format='json',
        )

    def test_nobody_refuses_group_creation_and_names_them(self):
        set_privacy(self.bob, group_adds_from=self.audience('NOBODY'))

        body = self.assertFailure(self.create_group(members=['bob']))

        self.assertIn('@bob', str(body['data']))
        self.assertEqual(Conversation.objects.filter(name='Trip').count(), 0)

    def test_nobody_refuses_add_members(self):
        group = Conversation.start_group(self.alice, name='Family')
        set_privacy(self.bob, group_adds_from=self.audience('NOBODY'))

        self.assertFailure(self.add_members(group, ['bob']))

        self.assertFalse(group.has_participant(self.bob))

    def test_loops_allows_somebody_from_a_shared_group(self):
        shared = Conversation.start_group(self.alice, name='Family')
        shared.add_participant(self.bob)
        set_privacy(self.bob, group_adds_from=self.audience('LOOPS'))

        self.assertSuccess(
            self.create_group(members=['bob']), status.HTTP_201_CREATED
        )

    def test_somebody_already_in_the_group_is_not_refused(self):
        # Re-adding a member is a no-op; refusing it would make an admin's
        # harmless repeat look like a rejection.
        group = Conversation.start_group(self.alice, name='Family')
        group.add_participant(self.bob)
        set_privacy(self.bob, group_adds_from=self.audience('NOBODY'))

        self.assertSuccess(self.add_members(group, ['bob']))

    def test_an_email_invitation_to_a_hidden_address_is_not_sent(self):
        # Hidden by "Find me using my email": nothing added, nothing emailed,
        # and no row left behind that could be claimed later.
        set_privacy(self.bob, discoverable_by_email=False)
        group = Conversation.start_group(self.alice, name='Family')

        row = membership.invite_by_email(group, self.bob.email, self.alice)

        self.assertIsNone(row)
        self.assertFalse(
            Participant.objects.filter(
                conversation=group, email__iexact=self.bob.email
            ).exists()
        )
        self.assertFalse(group.has_participant(self.bob))

    def test_an_email_invitation_is_not_claimed_by_somebody_who_refuses_adds(self):
        group = Conversation.start_group(self.alice, name='Family')
        membership.invite_by_email(group, 'mara@example.com', self.alice)
        mara = make_user('mara')
        mara.email = 'mara@example.com'
        mara.save(update_fields=['email'])
        mara.profile.email_verified = True
        mara.profile.save(update_fields=['email_verified'])
        set_privacy(mara, group_adds_from=self.audience('NOBODY'))

        claimed = membership.claim_email_invitations(mara)

        self.assertEqual(claimed, 0)
        self.assertFalse(group.has_participant(mara))

    def test_a_phone_invitation_is_not_claimed_by_somebody_who_refuses_adds(self):
        group = Conversation.start_group(self.alice, name='Family')
        number = '+639171234567'
        group.add_phone_participant(number, display_name='Mom', added_by=self.alice)
        mom = make_user('mom', number)
        set_privacy(mom, group_adds_from=self.audience('NOBODY'))

        self.assertIsNone(group._claim_phone_participant(mom))
        self.assertFalse(group.has_participant(mom))


class PresencePrivacyTests(EnvelopeAssertions, APITestCase):
    """What other members are shown about who is online."""

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)

    def bob_as_alice_sees_him(self):
        self.client.force_authenticate(self.alice)
        for row in members_of(self.client, self.group.id):
            if (row.get('user') or {}).get('username') == 'bob':
                return row['user']['profile']
        return None

    def test_an_open_socket_shows_as_online(self):
        from user import presence

        presence.connected(self.bob)

        self.assertTrue(self.bob_as_alice_sees_him()['is_online'])

    def test_with_online_status_off_they_always_read_offline(self):
        from user import presence

        presence.connected(self.bob)
        set_privacy(self.bob, online_status_visible=False)

        profile = self.bob_as_alice_sees_him()
        self.assertFalse(profile['is_online'])
        self.assertIsNone(profile['last_seen_at'])

    def test_presence_nobody_refreshed_reads_offline(self):
        from datetime import timedelta

        from user import presence
        from user.models import Profile

        presence.connected(self.bob)
        Profile.objects.filter(user=self.bob).update(
            last_seen_at=timezone.now() - presence.STALE_AFTER - timedelta(seconds=1)
        )

        self.assertFalse(self.bob_as_alice_sees_him()['is_online'])


class ReadReceiptPrivacyTests(EnvelopeAssertions, APITestCase):
    """Settings → Privacy → Read receipts, which is a two-way bargain."""

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.message = create_message(self.group, self.alice, text='hello')

    def bob_reads(self):
        self.client.force_authenticate(self.bob)
        return self.client.post(
            reverse('chat:conversation-read', args=[self.group.id]), {},
            format='json',
        )

    def row_for(self, viewer, username):
        self.client.force_authenticate(viewer)
        for row in members_of(self.client, self.group.id):
            if (row.get('user') or {}).get('username') == username:
                return row
        return None

    def test_a_read_is_visible_by_default(self):
        self.bob_reads()

        self.assertEqual(
            self.row_for(self.alice, 'bob')['last_read_message'], self.message.pk
        )

    def test_with_their_receipts_off_nobody_sees_their_read(self):
        set_privacy(self.bob, read_receipts_enabled=False)
        self.bob_reads()

        self.assertIsNone(self.row_for(self.alice, 'bob')['last_read_message'])

    def test_with_your_own_receipts_off_you_see_nobody_elses(self):
        # The bargain: showing none means being shown none.
        self.bob_reads()
        set_privacy(self.alice, read_receipts_enabled=False)

        self.assertIsNone(self.row_for(self.alice, 'bob')['last_read_message'])

    def test_your_own_read_is_always_your_own_to_see(self):
        # It clears their own badge; it is not a receipt about anybody.
        set_privacy(self.bob, read_receipts_enabled=False)
        self.bob_reads()

        self.assertEqual(
            self.row_for(self.bob, 'bob')['last_read_message'], self.message.pk
        )

    def test_the_receipt_frame_is_not_broadcast_when_theirs_is_off(self):
        set_privacy(self.bob, read_receipts_enabled=False)

        with mock.patch('chat.services.send_to_group') as sent, \
                self.captureOnCommitCallbacks(execute=True):
            self.bob_reads()

        frames = [call.args[1]['type'] for call in sent.call_args_list]
        # Their own devices still hear, so a badge cleared here clears there.
        self.assertIn('conversation.read', frames)
        self.assertNotIn('conversation.read.by', frames)


class MessageMentionApiTests(EnvelopeAssertions, APITestCase):
    """Naming somebody, over the REST API a client really uses.

    The service-level rules are in notification/tests.py:MentionTests - who may be
    named, and who is silently dropped. This is the wire: what a client sends,
    what comes back, and what an edit does to it.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )
        self.client.force_authenticate(self.alice)

    def send(self, **extra):
        return self.client.post(
            reverse('chat:conversation-messages', args=[self.group.id]),
            {'text': '@bob dinner?', **extra},
            format='json',
        )

    def test_a_message_comes_back_with_who_it_names(self):
        body = self.assertSuccess(self.send(mentions=[self.bob_row.pk]), 201)

        self.assertEqual(
            body['mentions'],
            [
                {
                    'participant_id': self.bob_row.pk,
                    'user_id': self.bob.pk,
                    'username': 'bob',
                    'display_label': self.bob.profile.display_label,
                }
            ],
        )
        self.assertFalse(body['mentions_everyone'])

    def test_everyone_is_a_flag(self):
        body = self.assertSuccess(self.send(mention_everyone=True), 201)

        self.assertTrue(body['mentions_everyone'])
        self.assertEqual(body['mentions'], [])

    def test_the_words_carry_no_markup(self):
        """What the SMS and email copies will read."""
        body = self.assertSuccess(self.send(mentions=[self.bob_row.pk]), 201)

        self.assertEqual(body['text'], '@bob dinner?')

    def test_an_edit_restates_the_mentions(self):
        message_id = self.assertSuccess(
            self.send(mentions=[self.bob_row.pk]), 201
        )['id']

        body = self.assertSuccess(
            self.client.patch(
                reverse(
                    'chat:conversation-message-detail',
                    args=[self.group.id, message_id],
                ),
                {'text': 'dinner?', 'mentions': []},
                format='json',
            )
        )
        self.assertEqual(body['mentions'], [])

    def test_an_edit_that_says_nothing_about_mentions_keeps_them(self):
        """An older client sends only `text`; it must not clear them."""
        message_id = self.assertSuccess(
            self.send(mentions=[self.bob_row.pk]), 201
        )['id']

        body = self.assertSuccess(
            self.client.patch(
                reverse(
                    'chat:conversation-message-detail',
                    args=[self.group.id, message_id],
                ),
                {'text': '@bob dinner tonight?'},
                format='json',
            )
        )
        self.assertEqual(len(body['mentions']), 1)

    def test_a_deleted_message_names_nobody(self):
        message_id = self.assertSuccess(
            self.send(mentions=[self.bob_row.pk]), 201
        )['id']

        self.client.delete(
            reverse(
                'chat:conversation-message-detail', args=[self.group.id, message_id]
            )
        )
        listed = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-messages', args=[self.group.id])
            )
        )
        rows = listed['results'] if isinstance(listed, dict) else listed
        self.assertEqual(rows[0]['mentions'], [])


class MentionTests(TestCase):
    """Who a message names - and, more importantly, who it may not name.

    The service level: `chat/mentions.py` decides which ids may stand.
    MessageMentionApiTests below is the same rules over the REST API.
    """

    def setUp(self):
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.mallory = make_user('mallory')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.other = Conversation.start_group(self.mallory, name='Somewhere else')

        self.bob_row = Participant.objects.get(
            conversation=self.group, user=self.bob
        )
        self.outsider_row = Participant.objects.get(
            conversation=self.other, user=self.mallory
        )

    def test_a_mention_is_stored_against_the_membership(self):
        message = create_message(
            self.group, self.alice, text='@bob dinner?',
            mentions=[self.bob_row.pk],
        )

        self.assertEqual(
            [row.participant_id for row in message.mentions.all()],
            [self.bob_row.pk],
        )

    def test_a_membership_from_another_conversation_is_dropped(self):
        """The security half: a mention must not point across conversations."""
        message = create_message(
            self.group, self.alice, text='@mallory',
            mentions=[self.outsider_row.pk, self.bob_row.pk],
        )

        self.assertEqual(
            [row.participant_id for row in message.mentions.all()],
            [self.bob_row.pk],
        )

    def test_naming_yourself_is_not_a_mention(self):
        alice_row = Participant.objects.get(
            conversation=self.group, user=self.alice
        )
        message = create_message(
            self.group, self.alice, text='@alice', mentions=[alice_row.pk]
        )

        self.assertEqual(message.mentions.count(), 0)

    def test_a_member_who_left_is_dropped(self):
        self.bob_row.status = Participant.Status.LEFT
        self.bob_row.save(update_fields=['status'])

        message = create_message(
            self.group, self.alice, text='@bob', mentions=[self.bob_row.pk]
        )
        self.assertEqual(message.mentions.count(), 0)

    def test_everyone_is_a_flag_not_a_row_per_member(self):
        from chat.mentions import mentions_participant

        message = create_message(
            self.group, self.alice, text='@everyone dinner',
            mention_everyone=True,
        )

        self.assertTrue(message.mentions_everyone)
        self.assertEqual(message.mentions.count(), 0)
        self.assertTrue(mentions_participant(message, self.bob_row))

    def test_the_sender_is_not_mentioned_by_everyone(self):
        from chat.mentions import mentions_participant

        alice_row = Participant.objects.get(
            conversation=self.group, user=self.alice
        )
        message = create_message(
            self.group, self.alice, text='@everyone', mention_everyone=True
        )

        self.assertFalse(mentions_participant(message, alice_row))

    def test_setting_mentions_again_replaces_them(self):
        """An edit that drops a name has to drop the mention."""
        message = create_message(
            self.group, self.alice, text='@bob', mentions=[self.bob_row.pk]
        )

        set_mentions(message, [], everyone=False)
        self.assertEqual(message.mentions.count(), 0)

    def test_a_reply_names_the_person_it_answers(self):
        from chat.mentions import is_reply_to

        first = create_message(self.group, self.bob, text='I can host')
        answer = create_message(
            self.group, self.alice, text='Perfect', reply_to=first
        )

        self.assertTrue(is_reply_to(answer, self.bob_row))

    def test_answering_yourself_tells_nobody(self):
        from chat.mentions import is_reply_to

        first = create_message(self.group, self.bob, text='I can host')
        second = create_message(
            self.group, self.bob, text='Actually, maybe not', reply_to=first
        )

        self.assertFalse(is_reply_to(second, self.bob_row))

    def test_the_text_carries_no_markup(self):
        """SMS and email copies read as a person wrote them."""
        message = create_message(
            self.group, self.alice, text='@Bob can you bring bread',
            mentions=[self.bob_row.pk],
        )

        self.assertEqual(message.text, '@Bob can you bring bread')


class ExternalConversationTests(TestCase):
    """A one-to-one thread whose far side is an address, not an account."""

    def setUp(self):
        self.yul = User.objects.create_user('yul', 'yul@example.com', 'x')
        self.address = 'stewart@gmail.com'

    def test_it_is_a_direct_conversation_keyed_on_the_address(self):
        conversation, created = Conversation.start_direct_external(
            self.yul, self.address
        )

        self.assertTrue(created)
        self.assertEqual(conversation.kind, Conversation.Kind.DIRECT)
        self.assertTrue(conversation.is_external_direct)
        self.assertEqual(
            conversation.direct_key, f'{self.yul.pk}:email:{self.address}'
        )

    def test_opening_it_twice_returns_the_same_thread(self):
        first, _ = Conversation.start_direct_external(self.yul, self.address)
        second, created = Conversation.start_direct_external(self.yul, self.address)

        self.assertEqual(first.pk, second.pk)
        self.assertFalse(created)
        self.assertEqual(
            Participant.objects.filter(conversation=first, user__isnull=True).count(),
            1,
        )

    def test_the_address_is_normalised_so_case_cannot_fork_the_thread(self):
        first, _ = Conversation.start_direct_external(self.yul, self.address)
        second, _ = Conversation.start_direct_external(self.yul, '  Stewart@Gmail.com ')

        self.assertEqual(first.pk, second.pk)

    def test_two_senders_reaching_one_address_get_two_threads(self):
        other = User.objects.create_user('ada', 'ada@example.com', 'x')

        mine, _ = Conversation.start_direct_external(self.yul, self.address)
        theirs, _ = Conversation.start_direct_external(other, self.address)

        self.assertNotEqual(mine.pk, theirs.pk)

    def test_the_far_side_starts_pending_and_is_named_by_its_local_part(self):
        conversation, _ = Conversation.start_direct_external(self.yul, self.address)

        row = Participant.objects.get(conversation=conversation, user__isnull=True)
        self.assertEqual(row.status, Participant.Status.ACTIVE)
        self.assertEqual(row.display_label, 'stewart')
        self.assertEqual(
            ChannelConsent.objects.get(
                participant=row, channel=ChannelConsent.Channel.EMAIL
            ).status,
            ChannelConsent.Status.PENDING,
        )

    def test_an_account_pair_key_and_an_address_key_cannot_collide(self):
        ada = User.objects.create_user('ada2', 'ada2@example.com', 'x')
        pair, _ = Conversation.start_direct(self.yul, ada)
        external, _ = Conversation.start_direct_external(self.yul, self.address)

        self.assertNotEqual(pair.direct_key, external.direct_key)
        self.assertFalse(pair.is_external_direct)


class ExternalDeliveryRuleTests(TestCase):
    """What the delivery ladder says about each kind of conversation."""

    def setUp(self):
        self.yul = User.objects.create_user('yul', 'yul@example.com', 'x')
        self.ada = User.objects.create_user('ada', 'ada@example.com', 'x')

    def test_a_group_is_carried_by_every_channel(self):
        group = Conversation.start_group(self.yul, name='Family')

        self.assertIsNone(delivery.conversation_reason(group))
        self.assertIsNone(delivery.conversation_reason(group, 'email'))
        self.assertIsNone(delivery.conversation_reason(group, 'sms'))

    def test_a_conversation_between_two_accounts_is_still_carried_by_none(self):
        """The regression DirectMessagesAreNotEmailedTests guards."""
        pair, _ = Conversation.start_direct(self.yul, self.ada)

        self.assertIsNotNone(delivery.conversation_reason(pair))
        self.assertIsNotNone(delivery.conversation_reason(pair, 'email'))
        self.assertIsNotNone(delivery.conversation_reason(pair, 'sms'))

    def test_an_external_thread_is_carried_by_email_and_not_by_sms(self):
        external, _ = Conversation.start_direct_external(self.yul, 'x@gmail.com')

        self.assertIsNone(delivery.conversation_reason(external, 'email'))
        self.assertIsNotNone(delivery.conversation_reason(external, 'sms'))


class ExternalBlockTests(TestCase):
    """Blocking the address on the far side, in both directions."""

    def setUp(self):
        self.yul = User.objects.create_user('yul', 'yul@example.com', 'x')
        self.address = 'stewart@gmail.com'
        self.conversation, _ = Conversation.start_direct_external(
            self.yul, self.address
        )
        self.row = Participant.objects.get(
            conversation=self.conversation, user__isnull=True
        )

    def test_the_blocker_is_told_plainly(self):
        blocks.block(self.yul, email=self.address)

        with self.assertRaises(PostingNotAllowed) as caught:
            create_message(self.conversation, self.yul, text='hello')

        self.assertIn('You blocked', str(caught.exception))

    def test_the_blocked_address_is_told_nothing(self):
        blocks.block(self.yul, email=self.address)

        with self.assertRaises(PostingNotAllowed) as caught:
            create_message(
                self.conversation, None, text='hello', participant=self.row
            )

        self.assertNotIn('blocked', str(caught.exception).lower())

    def test_an_unblocked_address_may_still_post(self):
        message = create_message(
            self.conversation, None, text='hello', participant=self.row
        )

        self.assertEqual(message.text, 'hello')


class ExternalRekeyTests(TestCase):
    """What happens when the address finally gets an account."""

    def setUp(self):
        self.yul = User.objects.create_user('yul', 'yul@example.com', 'x')
        self.address = 'stewart@gmail.com'
        self.conversation, _ = Conversation.start_direct_external(
            self.yul, self.address
        )

    def sign_up_stewart(self):
        stewart = User.objects.create_user('stewart', self.address, 'x')
        stewart.profile.email_verified = True
        stewart.profile.save(update_fields=['email_verified'])
        membership.claim_email_invitations(stewart)
        return stewart

    def test_the_thread_is_refiled_under_the_pair(self):
        stewart = self.sign_up_stewart()

        self.conversation.refresh_from_db()
        self.assertFalse(self.conversation.is_external_direct)
        self.assertEqual(
            self.conversation.direct_key,
            Conversation.direct_key_for(self.yul, stewart),
        )

    def test_start_direct_then_finds_the_thread_they_already_have(self):
        """Without the re-key this opens a SECOND thread - the whole point."""
        stewart = self.sign_up_stewart()

        found, created = Conversation.start_direct(self.yul, stewart)

        self.assertFalse(created)
        self.assertEqual(found.pk, self.conversation.pk)

    def test_a_thread_that_already_exists_absorbs_the_bridge(self):
        stewart = User.objects.create_user('stewart', 'other@example.com', 'x')
        pair, _ = Conversation.start_direct(self.yul, stewart)
        create_message(self.conversation, self.yul, text='sent by email')

        stewart.email = self.address
        stewart.save(update_fields=['email'])
        stewart.profile.email_verified = True
        stewart.profile.save(update_fields=['email_verified'])
        membership.claim_email_invitations(stewart)

        self.assertFalse(
            Conversation.objects.filter(pk=self.conversation.pk).exists()
        )
        self.assertEqual(pair.messages.filter(text='sent by email').count(), 1)


class ConsentIsRecordedPerChannelTests(TestCase):
    """The question, and the answer, belong to one channel each.

    This is the whole point of moving the record off Participant. The same
    person can be invited to a group by text in January and emailed about it in
    March. Those are two different questions, asked on two different days, in
    two different sets of words - and a single pair of fields on the member
    could only ever hold whichever came last.

    Silently overwriting the first is worse than never recording it, because
    nothing afterwards shows that anything was lost.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.participant = self.group.add_phone_participant(
            '+639170000002', display_name='Mom'
        )

    def test_two_channels_keep_two_separate_questions(self):
        membership.record_consent_asked(
            self.participant, ChannelConsent.Channel.SMS, 'Reply YES to get texts.'
        )
        membership.record_consent_asked(
            self.participant, ChannelConsent.Channel.EMAIL, 'Reply YES to get emails.'
        )

        sms = ChannelConsent.objects.get(
            participant=self.participant, channel=ChannelConsent.Channel.SMS
        )
        email = ChannelConsent.objects.get(
            participant=self.participant, channel=ChannelConsent.Channel.EMAIL
        )

        self.assertEqual(sms.consent_text, 'Reply YES to get texts.')
        self.assertEqual(email.consent_text, 'Reply YES to get emails.')
        self.assertIsNotNone(sms.asked_at)
        self.assertIsNotNone(email.asked_at)

    def test_asking_again_does_not_rewrite_the_first_question(self):
        # A resend is the same question. The date that matters is when they
        # were first put on notice, and the wording is what they first saw.
        membership.record_consent_asked(
            self.participant, ChannelConsent.Channel.SMS, 'The original wording.'
        )
        first = ChannelConsent.objects.get(
            participant=self.participant, channel=ChannelConsent.Channel.SMS
        )

        membership.record_consent_asked(
            self.participant, ChannelConsent.Channel.SMS, 'A later, different wording.'
        )

        again = ChannelConsent.objects.get(
            participant=self.participant, channel=ChannelConsent.Channel.SMS
        )
        self.assertEqual(again.consent_text, 'The original wording.')
        self.assertEqual(again.asked_at, first.asked_at)

    def test_answering_one_channel_leaves_the_other_alone(self):
        membership.record_consent_asked(
            self.participant, ChannelConsent.Channel.SMS, 'Reply YES to get texts.'
        )
        membership.record_consent_asked(
            self.participant, ChannelConsent.Channel.EMAIL, 'Reply YES to get emails.'
        )

        membership.set_channel_consent(
            self.participant,
            ChannelConsent.Channel.SMS,
            ChannelConsent.Status.OPTED_IN,
        )

        sms = ChannelConsent.objects.get(
            participant=self.participant, channel=ChannelConsent.Channel.SMS
        )
        email = ChannelConsent.objects.get(
            participant=self.participant, channel=ChannelConsent.Channel.EMAIL
        )
        self.assertEqual(sms.status, ChannelConsent.Status.OPTED_IN)
        self.assertIsNotNone(sms.consented_at)
        # Agreeing to texts is not agreeing to email.
        self.assertEqual(email.status, ChannelConsent.Status.PENDING)
        self.assertIsNone(email.consented_at)

    def test_a_texted_yes_records_when_they_agreed(self):
        # The gap that started this: YES set the status and nothing recorded
        # the date, because the only writer was a member-wide field the SMS
        # path never touched.
        membership.set_channel_consent(
            self.participant,
            ChannelConsent.Channel.SMS,
            ChannelConsent.Status.OPTED_IN,
        )

        sms = ChannelConsent.objects.get(
            participant=self.participant, channel=ChannelConsent.Channel.SMS
        )
        self.assertIsNotNone(sms.consented_at)


class NudgeAnUnansweredInvitationTests(EnvelopeAssertions, APITestCase):
    """Asking again, once a day at most, and never after an answer.

    A nudge is a message to somebody who has NOT consented, which is the
    traffic carriers police hardest. So it is admin-triggered rather than
    automatic, rate limited, and refused outright for anybody who has already
    said no - a second ask after a NO is the behaviour that loses a campaign.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.bob = make_user('bob', '+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.row = self.group.add_phone_participant('+639170000003', display_name='Mom')
        membership.record_consent_asked(
            self.row, ChannelConsent.Channel.SMS, 'You were added. Reply YES.'
        )
        self.client.force_authenticate(self.alice)
        self.url = reverse('chat:conversation-nudge', args=[self.group.pk])

    def send(self, channel=ChannelConsent.Channel.SMS, participant=None):
        return self.client.post(
            self.url,
            {'participant': (participant or self.row).pk, 'channel': channel},
            format='json',
        )

    def age_the_ask(self, hours):
        consent = ChannelConsent.objects.get(
            participant=self.row, channel=ChannelConsent.Channel.SMS
        )
        consent.asked_at = timezone.now() - timedelta(hours=hours)
        consent.save(update_fields=['asked_at'])
        return consent

    # --- the rate limit ---------------------------------------------------

    def test_a_fresh_invitation_cannot_be_nudged_yet(self):
        self.assertFailure(self.send())

    def test_a_day_later_it_can(self):
        self.age_the_ask(25)

        self.assertSuccess(self.send())

        consent = ChannelConsent.objects.get(
            participant=self.row, channel=ChannelConsent.Channel.SMS
        )
        self.assertIsNotNone(consent.reminded_at)

    def test_nudging_twice_in_a_day_is_refused(self):
        self.age_the_ask(25)
        self.assertSuccess(self.send())

        # The clock restarts from the nudge, not from the original ask.
        self.assertFailure(self.send())

    def test_the_first_ask_is_never_rewritten(self):
        consent = self.age_the_ask(25)
        first_asked, first_text = consent.asked_at, consent.consent_text

        self.send()

        consent.refresh_from_db()
        self.assertEqual(consent.asked_at, first_asked)
        self.assertEqual(consent.consent_text, first_text)

    # --- never after an answer -------------------------------------------

    def test_somebody_who_said_no_is_never_nudged(self):
        self.age_the_ask(72)
        membership.set_channel_consent(
            self.row, ChannelConsent.Channel.SMS, ChannelConsent.Status.DECLINED
        )

        self.assertFailure(self.send())

    def test_somebody_who_sent_stop_is_never_nudged(self):
        self.age_the_ask(72)
        membership.set_channel_consent(
            self.row, ChannelConsent.Channel.SMS, ChannelConsent.Status.OPTED_OUT
        )

        self.assertFailure(self.send())

    def test_somebody_who_already_agreed_is_not_nudged(self):
        self.age_the_ask(72)
        membership.set_channel_consent(
            self.row, ChannelConsent.Channel.SMS, ChannelConsent.Status.OPTED_IN
        )

        self.assertFailure(self.send())

    # --- who may do it ----------------------------------------------------

    def test_an_ordinary_member_cannot_nudge(self):
        self.age_the_ask(25)
        self.client.force_authenticate(self.bob)

        self.assertFailure(self.send())

    # --- per channel ------------------------------------------------------

    def test_nudging_sms_does_not_touch_the_email_row(self):
        membership.record_consent_asked(
            self.row, ChannelConsent.Channel.EMAIL, 'Emailed invitation.'
        )
        self.age_the_ask(25)

        self.send(ChannelConsent.Channel.SMS)

        email = ChannelConsent.objects.get(
            participant=self.row, channel=ChannelConsent.Channel.EMAIL
        )
        self.assertIsNone(email.reminded_at)

    # --- only a text goes out -----------------------------------------------

    def test_a_nudge_about_email_is_refused_and_texts_nobody(self):
        # The row 0028 left behind: the text invitation's date on the email row,
        # old enough to pass the floor. This used to go out as a TEXT, past the
        # SMS row's own floor.
        email = membership.record_consent_asked(
            self.row, ChannelConsent.Channel.EMAIL, 'You were added. Reply YES.'
        )
        email.asked_at = timezone.now() - timedelta(hours=25)
        email.save(update_fields=['asked_at'])

        with mock.patch('sms.outbound.send_notice') as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertFailure(self.send(ChannelConsent.Channel.EMAIL))

        sent.assert_not_called()
        email.refresh_from_db()
        self.assertIsNone(email.reminded_at)

    def test_somebody_with_no_number_is_not_nudged(self):
        # Otherwise the reminder is stamped and reported sent, and send_notice
        # quietly finds nobody to text.
        row = self.group.add_email_participant('mom@example.com')
        sms = membership.record_consent_asked(
            row, ChannelConsent.Channel.SMS, 'You were added. Reply YES.'
        )
        sms.asked_at = timezone.now() - timedelta(hours=25)
        sms.save(update_fields=['asked_at'])

        with mock.patch('sms.outbound.send_notice') as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertFailure(self.send(participant=row))

        sent.assert_not_called()
        sms.refresh_from_db()
        self.assertIsNone(sms.reminded_at)


class ChannelsOnTheMemberListTests(EnvelopeAssertions, APITestCase):
    """What the member list says about each channel, and when to offer a nudge.

    A member without an account is reachable at the one number or address they
    were added with. Their row for any other channel is seeded PENDING, and the
    0028 backfill copied the invitation date onto it - so somebody added by
    phone read "Email pending" and was offered a nudge about email.
    """

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '+639170000001')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.client.force_authenticate(self.alice)
        self.url = reverse('chat:conversation-participants', args=[self.group.pk])

    def channels_of(self, participant):
        data = self.assertSuccess(self.client.get(self.url))
        row = next(r for r in data['results'] if r['id'] == participant.pk)
        return row['channels']

    def asked_a_day_ago(self, participant):
        """Every row asked, and past the floor - what 0028 left behind."""
        ChannelConsent.objects.filter(participant=participant).update(
            asked_at=timezone.now() - timedelta(hours=25)
        )

    def test_somebody_added_by_phone_has_no_email_channel(self):
        row = self.group.add_phone_participant('+639170000003', display_name='Mom')
        self.asked_a_day_ago(row)

        channels = self.channels_of(row)

        self.assertIsNone(channels['email']['status'])
        self.assertIsNone(channels['email']['asked_at'])
        self.assertFalse(channels['email']['can_nudge'])
        self.assertEqual(channels['sms']['status'], 'pending')

    def test_somebody_added_by_email_has_no_text_channel(self):
        row = self.group.add_email_participant('mom@example.com')
        self.asked_a_day_ago(row)

        channels = self.channels_of(row)

        self.assertIsNone(channels['sms']['status'])
        self.assertFalse(channels['sms']['can_nudge'])
        self.assertEqual(channels['email']['status'], 'pending')

    def test_an_unanswered_text_invitation_offers_a_nudge(self):
        row = self.group.add_phone_participant('+639170000003', display_name='Mom')
        self.asked_a_day_ago(row)

        self.assertTrue(self.channels_of(row)['sms']['can_nudge'])

    def test_the_daily_floor_is_left_to_the_endpoint(self):
        # Offered straight after the invitation: pressing it is what says how
        # long to wait, which an absent entry could not.
        row = self.group.add_phone_participant('+639170000003', display_name='Mom')
        membership.record_consent_asked(
            row, ChannelConsent.Channel.SMS, 'You were added. Reply YES.'
        )

        self.assertTrue(self.channels_of(row)['sms']['can_nudge'])

    def test_no_nudge_once_they_have_answered(self):
        row = self.group.add_phone_participant('+639170000003', display_name='Mom')
        self.asked_a_day_ago(row)
        membership.set_channel_consent(
            row, ChannelConsent.Channel.SMS, ChannelConsent.Status.OPTED_IN
        )

        self.assertFalse(self.channels_of(row)['sms']['can_nudge'])

    def test_an_email_invitation_offers_no_nudge(self):
        # There is only a text reminder, and a nudge "about email" would send it.
        row = self.group.add_email_participant('mom@example.com')
        self.asked_a_day_ago(row)

        self.assertFalse(self.channels_of(row)['email']['can_nudge'])


class SearchMembersToAddTests(EnvelopeAssertions, APITestCase):
    """Finding people to add, and saying what adding each one would do.

    It must find nobody the rest of the product would not: username by prefix,
    email only exact and verified, nobody hidden by Settings > Privacy, neither
    side of a block. And a "ready" must not be contradicted by the add.
    """

    def setUp(self):
        cache.clear()
        from user.models import PrivacySettings

        self.audience = PrivacySettings.Audience
        self.alice = make_user('alice', '+639170000001')
        self.carol = make_user('carol', '+639170000003')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.carol)
        self.client.force_authenticate(self.alice)
        self.url = reverse('chat:conversation-search-members', args=[self.group.pk])

    def search(self, term):
        return self.assertSuccess(self.client.get(self.url, {'q': term}))

    def usernames(self, data):
        return [row['user']['username'] for row in data['results']]

    def verified(self, user):
        user.profile.email_verified = True
        user.profile.save(update_fields=['email_verified'])
        return user

    # --- by username ---------------------------------------------------------

    def test_a_username_prefix_lists_matching_people_with_their_picture(self):
        make_user('bobby')
        make_user('bob')

        data = self.search('bo')

        self.assertEqual(self.usernames(data), ['bob', 'bobby'])
        self.assertIn('avatar_url', data['results'][0]['user']['profile'])
        self.assertEqual(data['results'][0]['status'], 'ready')

    def test_at_most_five_are_shown_and_it_says_there_are_more(self):
        for i in range(7):
            make_user(f'sam{i}')

        data = self.search('sam')

        self.assertEqual(len(data['results']), 5)
        self.assertTrue(data['has_more'])

    def test_one_letter_finds_nobody(self):
        make_user('bob')

        self.assertEqual(self.search('b')['results'], [])

    def test_the_searcher_is_not_offered_to_themselves(self):
        self.assertEqual(self.usernames(self.search('ali')), [])

    # --- by email ------------------------------------------------------------

    def test_an_exact_verified_email_finds_the_account(self):
        self.verified(make_user('bob'))

        data = self.search('BOB@example.com')

        self.assertEqual(self.usernames(data), ['bob'])
        self.assertFalse(data['has_more'])

    def test_part_of_an_email_finds_nobody(self):
        # A prefix over addresses would walk the directory a letter at a time.
        self.verified(make_user('bob'))

        self.assertEqual(self.search('bob@exam')['results'], [])

    def test_an_unverified_email_finds_nobody(self):
        make_user('bob')

        self.assertEqual(self.search('bob@example.com')['results'], [])

    def test_an_email_hidden_in_privacy_finds_nobody(self):
        bob = self.verified(make_user('bob'))
        set_privacy(bob, discoverable_by_email=False)

        self.assertEqual(self.search('bob@example.com')['results'], [])

    # --- an address with no account ---------------------------------------------

    def test_a_whole_address_nobody_has_is_offered_for_an_invitation(self):
        data = self.search('Ana@Example.com')

        self.assertEqual(data['results'], [])
        self.assertEqual(
            data['invite'],
            {'email': 'ana@example.com', 'status': 'ready', 'detail': ''},
        )

    def test_part_of_an_address_is_not_offered(self):
        self.assertIsNone(self.search('ana@exam')['invite'])

    def test_an_address_already_invited_says_so(self):
        self.group.add_email_participant('ana@example.com')

        self.assertEqual(
            self.search('ana@example.com')['invite']['status'], 'already_member'
        )

    def test_a_hidden_account_is_offered_like_any_other_address(self):
        # Anything else would say an account is there. The invitation itself
        # then adds and emails nobody.
        bob = self.verified(make_user('bob'))
        set_privacy(bob, discoverable_by_email=False)

        self.assertEqual(self.search('bob@example.com')['invite']['status'], 'ready')

    def test_your_own_address_is_you_not_an_invitation(self):
        self.verified(self.alice)

        data = self.search('alice@example.com')

        self.assertEqual(self.usernames(data), ['alice'])
        self.assertEqual(data['results'][0]['status'], 'already_member')
        self.assertIsNone(data['invite'])

    # --- what adding them would do ---------------------------------------------

    def test_somebody_already_here_says_so(self):
        data = self.search('car')

        self.assertEqual(data['results'][0]['status'], 'already_member')

    def test_somebody_who_refuses_group_adds_says_so(self):
        bob = make_user('bob')
        set_privacy(bob, group_adds_from=self.audience.NOBODY)

        row = self.search('bob')['results'][0]

        self.assertEqual(row['status'], 'refused')
        self.assertIn('@bob', row['detail'])

    def test_somebody_blocked_from_this_loop_says_so(self):
        bob = make_user('bob')
        row = self.group.add_participant(bob)
        row.status = Participant.Status.BLOCKED
        row.save(update_fields=['status'])

        self.assertEqual(self.search('bob')['results'][0]['status'], 'refused')

    def test_either_side_of_a_block_is_not_found(self):
        bob = make_user('bob')
        blocks.block(bob, user=self.alice)

        self.assertEqual(self.search('bob')['results'], [])

    def test_a_ready_result_can_be_added(self):
        make_user('bob')
        self.assertEqual(self.search('bob')['results'][0]['status'], 'ready')

        added = self.client.post(
            reverse('chat:conversation-members', args=[self.group.pk]),
            {'members': ['bob']},
            format='json',
        )

        self.assertSuccess(added)

    # --- who may ask -----------------------------------------------------------

    def test_it_adds_nobody(self):
        make_user('bob')
        before = self.group.active_participants.count()

        self.search('bob')

        self.assertEqual(self.group.active_participants.count(), before)

    def test_an_ordinary_member_cannot_search_for_people_to_add(self):
        self.client.force_authenticate(self.carol)

        self.assertFailure(
            self.client.get(self.url, {'q': 'bob'}), status.HTTP_403_FORBIDDEN
        )


class GroupCategoryAndRegionTests(EnvelopeAssertions, APITestCase):
    """What kind of group and where (slice 4 of docs/campaigns_final_plan.md):
    an admin declares them in the group's settings, and they are the only two
    facts about a group a campaign targets on."""

    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', phone='+639170000001')
        self.bob = make_user('bob', phone='+639170000002')
        self.group = Conversation.start_group(self.alice, name='Family')
        self.group.add_participant(self.bob)
        self.url = reverse('chat:conversation-settings', args=[self.group.id])

    def patch(self, user, **group_settings):
        self.client.force_authenticate(user)
        return self.client.patch(self.url, {'settings': group_settings}, format='json')

    def test_an_admin_declares_them_and_every_member_reads_them(self):
        data = self.assertSuccess(self.patch(self.alice, category='school', region='ph-00'))
        self.assertEqual(
            (data['settings']['category'], data['settings']['region']), ('school', 'PH-00')
        )
        self.group.refresh_from_db()
        self.assertEqual((self.group.category.slug, self.group.region), ('school', 'PH-00'))

        self.client.force_authenticate(self.bob)
        read = self.assertSuccess(
            self.client.get(reverse('chat:conversation-detail', args=[self.group.id]))
        )
        self.assertEqual(read['settings']['category'], 'school')

    def test_a_country_alone_is_a_place_and_both_can_be_taken_back(self):
        self.assertSuccess(self.patch(self.alice, category='school', region='PH'))
        self.assertSuccess(self.patch(self.alice, category=None, region=''))
        self.group.refresh_from_db()
        self.assertEqual((self.group.category, self.group.region), (None, ''))

    def test_only_a_known_place_and_a_current_category(self):
        self.assertFailure(self.patch(self.alice, region='PH-99'))
        self.assertFailure(self.patch(self.alice, region='Manila'))
        self.assertFailure(self.patch(self.alice, region='JP'))
        GroupCategory.objects.filter(slug='school').update(active=False)
        self.assertFailure(self.patch(self.alice, category='school'))
        self.group.refresh_from_db()
        self.assertEqual((self.group.category, self.group.region), (None, ''))

    def test_a_member_cannot_declare_them_even_where_members_edit_the_group(self):
        self.group.edit_group_permission = 'members'
        self.group.save(update_fields=['edit_group_permission'])
        self.assertFailure(self.patch(self.bob, category='school'), status.HTTP_403_FORBIDDEN)
        self.assertFailure(self.patch(self.bob, region='PH'), status.HTTP_403_FORBIDDEN)

    def test_a_new_group_can_say_them_from_the_start(self):
        self.client.force_authenticate(self.alice)
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-group'),
                {'name': 'Choir', 'settings': {'category': 'religious', 'region': 'PH-07'}},
                format='json',
            ),
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            (data['settings']['category'], data['settings']['region']), ('religious', 'PH-07')
        )

    def test_the_choices_are_listed_for_anybody_signed_in(self):
        self.client.force_authenticate(self.bob)
        data = self.assertSuccess(self.client.get(reverse('chat:conversation-group-options')))
        self.assertIn({'slug': 'school', 'label': 'School'}, data['categories'])
        philippines = next(c for c in data['countries'] if c['code'] == 'PH')
        self.assertIn({'code': 'PH-00', 'name': 'Metro Manila (NCR)'}, philippines['regions'])
