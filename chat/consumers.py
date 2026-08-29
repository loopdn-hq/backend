"""The realtime half of chat.

A client opens one socket at `/ws/chat/?token=<access token>` and is
subscribed to every conversation it belongs to, plus a group of its own for
events that are about the user rather than a conversation.

    <- {"type": "message.new", "message": {...}}
    <- {"type": "conversation.added", "conversation": {...}}
    <- {"type": "typing", "conversation_id": 1, "user_id": 2}

    -> {"type": "message.send", "conversation_id": 1, "text": "hi"}
    -> {"type": "typing", "conversation_id": 1}

Attachments are not sent over the socket - binary uploads belong on the REST
endpoint, which broadcasts the finished message here anyway.
"""
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from chat.models import Conversation, Participant
from chat.services import (
    conversation_group,
    create_message,
    serialize_message,
    user_group,
)

# Close codes. 4401/4403 mirror HTTP 401/403 in the private 4000+ range.
CLOSE_UNAUTHENTICATED = 4401


class ChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if user is None or not user.is_authenticated:
            # Reject before accepting: an unauthenticated socket has no
            # business being open at all.
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        self.user = user
        self.subscribed = set()

        await self.accept()

        await self.subscribe(user_group(user.pk))
        for conversation_id in await self.conversation_ids():
            await self.subscribe(conversation_group(conversation_id))

    async def disconnect(self, code):
        for group in list(getattr(self, 'subscribed', ())):
            await self.channel_layer.group_discard(group, self.channel_name)

    async def subscribe(self, group):
        await self.channel_layer.group_add(group, self.channel_name)
        self.subscribed.add(group)

    # -- inbound ----------------------------------------------------------

    async def receive_json(self, content, **kwargs):
        handlers = {
            'message.send': self.handle_message_send,
            'typing': self.handle_typing,
        }
        handler = handlers.get(content.get('type'))
        if handler is None:
            await self.send_json({'type': 'error', 'detail': 'Unknown message type.'})
            return
        await handler(content)

    async def handle_message_send(self, content):
        conversation = await self.get_conversation(content.get('conversation_id'))
        if conversation is None:
            # Same answer for "does not exist" and "not yours", so a socket
            # cannot be used to probe which conversations exist.
            await self.send_json({'type': 'error', 'detail': 'No such conversation.'})
            return

        text = (content.get('text') or '').strip()
        if not text:
            await self.send_json({'type': 'error', 'detail': 'Message text is required.'})
            return

        # create_message broadcasts to the conversation group, which this
        # socket is part of - so the sender's own echo arrives that way too.
        await self.store_message(conversation, text)

    async def handle_typing(self, content):
        conversation = await self.get_conversation(content.get('conversation_id'))
        if conversation is None:
            return
        await self.channel_layer.group_send(
            conversation_group(conversation.pk),
            {
                'type': 'typing',
                'conversation_id': conversation.pk,
                'user_id': self.user.pk,
            },
        )

    # -- outbound (channel layer event -> socket) -------------------------

    async def message_new(self, event):
        await self.send_json({'type': 'message.new', 'message': event['message']})

    async def conversation_added(self, event):
        # Join it now, so messages start arriving without a reconnect.
        await self.subscribe(conversation_group(event['conversation_id']))
        await self.send_json(
            {'type': 'conversation.added', 'conversation': event['conversation']}
        )

    async def typing(self, event):
        if event['user_id'] == self.user.pk:
            return  # nobody needs to be told they are typing
        await self.send_json(
            {
                'type': 'typing',
                'conversation_id': event['conversation_id'],
                'user_id': event['user_id'],
            }
        )

    # -- database ---------------------------------------------------------

    @database_sync_to_async
    def conversation_ids(self):
        return list(
            Participant.objects.filter(user=self.user).values_list(
                'conversation_id', flat=True
            )
        )

    @database_sync_to_async
    def get_conversation(self, conversation_id):
        if not conversation_id:
            return None
        return Conversation.objects.filter(
            pk=conversation_id, participants__user=self.user
        ).first()

    @database_sync_to_async
    def store_message(self, conversation, text):
        return serialize_message(create_message(conversation, self.user, text=text))
