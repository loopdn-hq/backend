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
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from chat.models import (
    BlockedFromConversation,
    Conversation,
    Participant,
    PostingNotAllowed,
)
from chat.services import (
    conversation_group,
    create_message,
    serialize_message,
    user_group,
)

# Close codes. 4401/4403 mirror HTTP 401/403 in the private 4000+ range.
logger = logging.getLogger(__name__)

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

        # Subscribe BEFORE accepting. `accept()` is what fires `open` on the
        # client, and a client that hears `open` starts sending and expects to
        # receive - so accepting first left a window where a message broadcast
        # to this conversation reached nobody on this socket. Nothing can be
        # delivered during the window either: Channels dispatches one event at
        # a time, so no group message is handled until connect() returns.
        await self.subscribe(user_group(user.pk))
        for conversation_id in await self.conversation_ids():
            await self.subscribe(conversation_group(conversation_id))

        await self.accept()

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

        # An exception raised in here escapes the ASGI application and Daphne
        # tears the socket down - Channels' consumer catches only StopConsumer.
        # So a member posting into a broadcast group did not get told "only
        # admins can post": their connection dropped, they reconnected, and
        # everything broadcast in the gap was lost. Any handler bug had the
        # same effect.
        try:
            await handler(content)
        except (PostingNotAllowed, BlockedFromConversation) as refusal:
            # An ordinary, expected refusal. The client is told, and the socket
            # stays up.
            await self.send_json({'type': 'error', 'detail': str(refusal)})
        except Exception:
            # A bug. Logged with its traceback because nothing else will report
            # it - but still not a reason to disconnect somebody mid-conversation.
            logger.exception(
                'Websocket handler %r failed for user %s',
                content.get('type'), getattr(self.user, 'pk', None),
            )
            await self.send_json(
                {'type': 'error', 'detail': 'Something went wrong. Try again.'}
            )

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

        # `client_id` is the sender's own id for this message. A websocket send
        # is not acknowledged, so a client whose socket drops mid-send cannot
        # know whether it landed; keying the send is what makes its retry safe
        # rather than a second row and a second billed SMS fan-out. Optional,
        # so a client that does not send one behaves exactly as before.
        client_id = str(content.get('client_id') or '')[:64]

        # create_message broadcasts to the conversation group, which this
        # socket is part of - so the sender's own echo arrives that way too.
        await self.store_message(conversation, text, client_id=client_id)

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

    async def conversation_request(self, event):
        """Somebody is waiting to be let into a group this socket administers."""
        await self.send_json(
            {
                'type': 'conversation.request',
                'conversation_id': event['conversation_id'],
                'participant': event['participant'],
            }
        )

    async def conversation_removed(self, event):
        """Stop delivering a conversation this socket may no longer see.

        Discarding the group is what actually revokes access on a live socket;
        the client message is a courtesy so the UI can close the thread rather
        than let it sit there going quiet.
        """
        await self.channel_layer.group_discard(
            conversation_group(event['conversation_id']), self.channel_name
        )
        await self.send_json(
            {
                'type': 'conversation.removed',
                'conversation_id': event['conversation_id'],
            }
        )

    async def conversation_added(self, event):
        # Join it now, so messages start arriving without a reconnect.
        await self.subscribe(conversation_group(event['conversation_id']))
        await self.send_json(
            {'type': 'conversation.added', 'conversation': event['conversation']}
        )

    async def conversation_updated(self, event):
        """The group's settings changed - mode, join mode, permissions.

        The whole conversation is sent rather than a diff so the client applies
        one shape it already knows how to read.
        """
        await self.send_json(
            {
                'type': 'conversation.updated',
                'conversation_id': event['conversation_id'],
                'conversation': event['conversation'],
            }
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
        # Active memberships only. A removed or blocked member keeps their row,
        # and subscribing them to the channel group would push every future
        # message straight to the socket of somebody who was shown the door.
        return list(
            Participant.objects.filter(
                user=self.user, status=Participant.Status.ACTIVE
            ).values_list('conversation_id', flat=True)
        )

    @database_sync_to_async
    def get_conversation(self, conversation_id):
        if not conversation_id:
            return None
        return Conversation.objects.filter(
            pk=conversation_id,
            participants__user=self.user,
            participants__status=Participant.Status.ACTIVE,
        ).first()

    @database_sync_to_async
    def store_message(self, conversation, text, client_id=''):
        return serialize_message(
            create_message(
                conversation, self.user, text=text, client_id=client_id
            )
        )
