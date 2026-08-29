"""Creating messages and telling everyone about them.

Both entrypoints - the REST endpoint and the websocket consumer - call
`create_message`, so a message is stored and broadcast exactly one way no
matter how it arrived.
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from chat.models import Attachment, Message
from chat.utils import describe_upload


def conversation_group(conversation_id):
    return f'chat.conversation.{conversation_id}'


def user_group(user_id):
    return f'chat.user.{user_id}'


def send_to_group(group, payload):
    """Fire an event at a channel group from synchronous code.

    Silently does nothing if no channel layer is configured, so that the
    REST API keeps working even when realtime is switched off.
    """
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(group, payload)


@transaction.atomic
def create_message(conversation, sender, text='', uploads=None):
    """Store a message with its attachments and touch the conversation."""
    message = Message.objects.create(
        conversation=conversation,
        sender=sender,
        text=text or '',
    )

    for upload in uploads or []:
        Attachment.objects.create(
            message=message, file=upload, **describe_upload(upload)
        )

    # Moves the conversation to the top of everyone's list. auto_now fills
    # in the value.
    conversation.save(update_fields=['updated_at'])

    # Only broadcast once the row is really committed - subscribers on other
    # workers would otherwise race the transaction and fetch nothing.
    transaction.on_commit(lambda: broadcast_message(message))
    return message


def serialize_message(message):
    """The message payload shared by the REST response and the broadcast.

    No request is available inside a consumer, so attachment URLs come out
    relative here. Clients resolve them against the API host they already
    know.
    """
    from chat.serializers import MessageSerializer

    return MessageSerializer(message).data


def broadcast_message(message):
    send_to_group(
        conversation_group(message.conversation_id),
        {'type': 'message.new', 'message': serialize_message(message)},
    )


def broadcast_added_to_conversation(conversation, users):
    """Tell people they are now in a conversation, so their socket joins it.

    Sent to each person's own group because they are not subscribed to the
    conversation's group yet - that is the thing this event causes.
    """
    from chat.serializers import ConversationSerializer

    payload = ConversationSerializer(conversation).data
    for user in users:
        send_to_group(
            user_group(user.pk),
            {
                'type': 'conversation.added',
                'conversation_id': conversation.pk,
                'conversation': payload,
            },
        )
