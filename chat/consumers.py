"""The realtime half of chat.

A client opens one socket at `/ws/chat/?token=<access token>` and is
subscribed to every conversation it belongs to, plus a group of its own for
events that are about the user rather than a conversation.

    <- {"type": "message.new", "message": {...}}
    <- {"type": "conversation.added", "conversation": {...}}
    <- {"type": "notification.new", "notification": {...}}
    <- {"type": "typing", "conversation_id": 1, "user_id": 2}
    <- {"type": "conversation.read", "conversation_id": 1,
        "last_read_message_id": 42}   (this account, on another device)

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
    Message,
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

        # Same guard as the REST path, and for the same reason: a parent from
        # another conversation would come straight back inside the reply
        # excerpt, turning "send a message" into a way to read a group this
        # sender is not in. Resolved here rather than trusted.
        reply_to = await self.resolve_reply_to(
            conversation, content.get('reply_to')
        )
        if reply_to is False:
            await self.send_json(
                {
                    'type': 'error',
                    'detail': 'You can only reply to a message in this conversation.',
                }
            )
            return

        # create_message broadcasts to the conversation group, which this
        # socket is part of - so the sender's own echo arrives that way too.
        await self.store_message(
            conversation,
            text,
            client_id=client_id,
            reply_to=reply_to,
            # Membership ids the composer offered. Not trusted here: they are
            # checked against this conversation's ACTIVE members in
            # chat/mentions.py, the same place the REST path checks them.
            mentions=content.get('mentions'),
            mention_everyone=bool(content.get('mention_everyone')),
        )

    async def handle_typing(self, content):
        conversation = await self.get_conversation(content.get('conversation_id'))
        if conversation is None:
            return
        if await self.direct_contact_is_blocked(conversation):
            # A blocked direct thread is read-only, and a typing indicator is
            # the one signal that would still cross it - the other person would
            # watch them start and stop typing in a conversation neither can
            # send into. It is person-to-person contact by any reading, so it
            # stops with the rest.
            #
            # Silently, and only here: groups are untouched, and returning an
            # error would tell the sender something the refusals are careful
            # not to.
            return
        await self.channel_layer.group_send(
            conversation_group(conversation.pk),
            {
                'type': 'typing',
                'conversation_id': conversation.pk,
                'user_id': self.user.pk,
            },
        )

    @database_sync_to_async
    def direct_contact_is_blocked(self, conversation):
        """Whether this is a direct thread between two people who cannot talk.

        False for every group, always: a block is about direct contact and
        changes nothing about a conversation other people are also in.
        """
        from chat.models import Conversation, Participant
        from user.blocks import blocks_between


        if conversation.kind != Conversation.Kind.DIRECT:
            return False

        other = (
            Participant.objects.filter(
                conversation=conversation, user__isnull=False
            )
            .exclude(user=self.user)
            .select_related('user')
            .first()
        )
        if other is None:
            return False
        return blocks_between(self.user, other.user)

    # -- outbound (channel layer event -> socket) -------------------------

    async def message_new(self, event):
        await self.send_json({'type': 'message.new', 'message': event['message']})

    async def message_updated(self, event):
        """A message already in the thread changed - edit, delete, reaction.

        The whole message is sent, and the client replaces the one it holds
        with the same id. It must NOT append: this is not a new message.
        """
        await self.send_json(
            {'type': 'message.updated', 'message': event['message']}
        )

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

    async def notification_new(self, event):
        """A new activity notification for this account.

        Arrives on the user's own group. Without this method the frame was
        being dropped: Channels routes a group event by turning its `type` into
        a method name, so `notification.new` needs `notification_new` to exist
        or there is nothing to deliver it to. The row was still written, which
        is why the feed filled in on a refresh and never live.
        """
        await self.send_json(
            {
                'type': 'notification.new',
                'notification': event['notification'],
            }
        )

    async def conversation_read(self, event):
        """This account read a conversation somewhere else.

        Arrives on the user's own group, so every device this person has open
        hears it and can clear the badge together. Nobody else's socket is in
        that group, so nothing here is visible to the rest of the conversation.
        """
        await self.send_json(
            {
                'type': 'conversation.read',
                'conversation_id': event['conversation_id'],
                'last_read_message_id': event['last_read_message_id'],
            }
        )

    async def conversation_read_by(self, event):
        """Somebody else in this conversation has read up to a message.

        Arrives on the conversation's group, so everyone in it hears. The
        reader's own client ignores it - it already knows - and the senders use
        it to mark their messages seen.

        Withheld from anybody who turned their own Read receipts off: the
        bargain is two-way, so somebody who shows none is shown none. Asked per
        frame rather than cached at connect, because a socket can be open for
        hours and the setting must not take that long to take effect.
        """
        if event['user_id'] != self.user.pk and not await self.sees_read_receipts():
            return

        await self.send_json(
            {
                'type': 'conversation.read.by',
                'conversation_id': event['conversation_id'],
                'user_id': event['user_id'],
                'last_read_message_id': event['last_read_message_id'],
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
    def sees_read_receipts(self):
        """Whether this socket's account is shown other people's read receipts.

        Their own Read receipts switch: the bargain is two-way, so somebody who
        shows none is shown none (user/privacy.py).
        """
        from user import privacy

        return privacy.shows_read_receipts(self.user)

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
    def resolve_reply_to(self, conversation, message_id):
        """The parent for a reply: None when none, False when refused.

        Three-valued on purpose - "not replying" and "replying to something you
        may not see" are different answers, and only the second is an error.
        """
        if message_id in (None, ''):
            return None

        parent = Message.objects.filter(
            pk=message_id, conversation=conversation
        ).first()
        return parent if parent is not None else False

    @database_sync_to_async
    def store_message(self, conversation, text, client_id='', reply_to=None,
                      mentions=None, mention_everyone=False):
        return serialize_message(
            create_message(
                conversation,
                self.user,
                text=text,
                client_id=client_id,
                reply_to=reply_to,
                mentions=mentions,
                mention_everyone=mention_everyone,
            )
        )
