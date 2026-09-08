"""Tests for conversations, membership, messages and the realtime layer."""
import shutil
import tempfile
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
from django.test import TransactionTestCase, override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase, APITransactionTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from chat import membership
from sms import inbound
from chat.models import (
    Attachment,
    BlockedFromConversation,
    Conversation,
    Message,
    Notification,
    Participant,
)
from chat.serializers import ConversationSerializer
from chat.services import create_message
from user.tests import EnvelopeAssertions, make_image_upload

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

    def test_search_conversations_returns_joinable_groups_only(self):
        open_group = Conversation.start_group(self.alice, 'Weekend Plans')
        open_group.join_mode = Conversation.JoinMode.OPEN
        open_group.mode = Conversation.Mode.BROADCAST
        open_group.save(update_fields=['join_mode', 'mode'])
        Conversation.start_group(self.alice, 'Weekend Private')

        data = self.assertSuccess(
            self.client.get(
                reverse('chat:conversation-search'), {'q': 'weekend'}
            )
        )

        self.assertEqual(len(data['results']), 1)
        self.assertEqual(
            data['results'][0],
            {
                'id': open_group.id,
                'name': 'Weekend Plans',
                'join_mode': 'open',
                'mode': 'broadcast',
            },
        )

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
        from chat.services import create_message

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
        self.assertEqual(mom.sms_consent, Participant.Consent.PENDING)

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
            self.client.get(reverse('chat:notification-list'), params)
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
            self.client.post(reverse('chat:notification-read'), {})
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
                reverse('chat:notification-read'), {'ids': [theirs.pk]},
                format='json',
            )
        )

        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)

    def test_unread_filter(self):
        membership.set_role(self.bob_row(), Participant.Role.ADMIN, self.alice)
        self.client.force_authenticate(self.bob)
        self.client.post(reverse('chat:notification-read'), {})

        self.assertEqual(self.types_for(self.bob, unread='1'), [])


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

    def test_clearing_an_empty_conversation_is_harmless(self):
        empty = Conversation.start_group(self.alice, name='Quiet')

        self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-clear', args=[empty.id]), {}
            )
        )

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
