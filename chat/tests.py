"""Tests for conversations, membership, messages and the realtime layer."""
import shutil
import tempfile

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from chat.models import Attachment, Conversation, Message, Participant
from chat.services import create_message
from user.tests import EnvelopeAssertions, make_image_upload

MEDIA_ROOT = tempfile.mkdtemp(prefix='loopd-chat-media-')


def make_user(username, phone=None, password='a-strong-passphrase-42'):
    user = User.objects.create_user(
        username=username, email=f'{username}@example.com', password=password
    )
    if phone:
        user.profile.phone_number = phone
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
        self.assertEqual(len(data['participants']), 2)

    def test_start_direct_by_phone_number(self):
        data = self.assertSuccess(
            self.client.post(
                reverse('chat:conversation-direct'), {'identifier': '+639170000002'}
            )
        )
        usernames = {p['user']['username'] for p in data['participants']}
        self.assertEqual(usernames, {'alice', 'bob'})

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
        usernames = {p['user']['username'] for p in data['participants']}
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
        usernames = {p['user']['username'] for p in data['participants']}
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

    # An Origin header is required - AllowedHostsOriginValidator rejects a
    # handshake that has none.
    HEADERS = [(b'origin', b'http://testserver')]

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
