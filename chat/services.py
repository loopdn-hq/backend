"""Creating messages and telling everyone about them.

Both entrypoints - the REST endpoint and the websocket consumer - call
`create_message`, so a message is stored and broadcast exactly one way no
matter how it arrived.
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import IntegrityError, transaction

from chat.models import (
    Attachment,
    Conversation,
    Message,
    Participant,
    PostingNotAllowed,
)
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
def create_message(conversation, sender=None, text='', uploads=None, *,
                   source=Message.Source.APP, participant=None, client_id=''):
    """Store a message with its attachments and touch the conversation.

    `sender` is the account that sent it, or None when the sender has no
    account - somebody texting in from a number that is a member of the group.
    `participant` is the membership row they sent it as; it is resolved from
    `sender` when not given, so every existing caller keeps working unchanged
    and gets the attribution for free.

    The two are kept in step here rather than at each call site, because a
    message whose `sender` and `sender_participant` disagree would render one
    name in the app and credit a different one in the SMS fan-out.
    """
    if participant is None and sender is not None:
        # Explicit queryset, and explicitly ACTIVE: a message is credited to a
        # live membership or to none. Going through the relation would read the
        # prefetch cache, which may or may not be filtered depending on how the
        # caller fetched the conversation.
        participant = Participant.objects.filter(
            conversation=conversation,
            user=sender,
            status=Participant.Status.ACTIVE,
        ).first()

    # Enforced here rather than in each caller: this is the single write path
    # for REST, the websocket and inbound SMS, so a broadcast group that three
    # entry points had to remember to check would be a broadcast group in two
    # of them.
    if not conversation.allows_posting_by(participant):
        raise PostingNotAllowed('Only admins can post in this group.')

    if client_id:
        # A retry of something already stored. Returned rather than re-created,
        # and WITHOUT re-broadcasting or re-scheduling the SMS fan-out: the
        # first call already did both, and doing them twice would text every
        # member's phone a second time and charge for it.
        existing = Message.objects.filter(
            conversation=conversation, client_id=client_id
        ).first()
        if existing is not None:
            return existing

    try:
        # A SAVEPOINT, because this function is atomic: two sends racing on the
        # same client_id both pass the check above, and the loser's
        # IntegrityError would otherwise poison the whole transaction rather
        # than being recoverable. The unique index is the real guarantee; the
        # check above only saves the round trip in the common case.
        with transaction.atomic():
            message = Message.objects.create(
                conversation=conversation,
                client_id=client_id,
                sender=sender,
                sender_participant=participant,
                source=source,
                text=text or '',
            )
    except IntegrityError:
        duplicate = (
            Message.objects.filter(
                conversation=conversation, client_id=client_id
            ).first()
            if client_id
            else None
        )
        if duplicate is None:
            # Not the collision we are prepared for.
            raise
        return duplicate

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

    # And out to anybody taking part by text. Scheduled, never run inline: this
    # is N provider calls, and it must not happen inside the transaction that
    # is still holding the row - see sms/outbound.py:schedule.
    _schedule_sms_fanout(message)
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


def announce_added_to_conversation(conversation, users):
    """Queue the added-to-conversation event for after the transaction commits.

    Two bugs in one, both of which only appear on a real deployment:

    Publishing inside the transaction lets a socket on another worker receive
    the event, subscribe, and read a conversation that has not been committed
    yet - the same hazard `create_message` documents for message broadcasts.

    Serialising `conversation` as the caller holds it renders whatever its
    participant prefetch was populated with, which was fetched BEFORE the new
    people existed. Each new member is then told a member list that does not
    include them. Re-reading by pk after commit is what makes the payload true.
    """
    pks = [user.pk for user in users if user is not None]
    if not pks:
        return

    def _publish():
        from django.contrib.auth.models import User

        fresh = Conversation.objects.filter(pk=conversation.pk).first()
        if fresh is None:
            return
        broadcast_added_to_conversation(fresh, list(User.objects.filter(pk__in=pks)))

    transaction.on_commit(_publish)


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


def broadcast_removed_from_conversation(conversation, users):
    """Tell people they are no longer in a conversation, so their socket lets go.

    Subscription is decided once, at connect, from the memberships that existed
    then. Without this event a member who is removed or blocked keeps receiving
    every message on the socket they already hold - for as long as they leave
    the tab open - while every REST route correctly refuses them. The one place
    the status column could not reach on its own.

    Sent to each person's own group, because the thing being revoked is their
    membership of the conversation group.
    """
    for user in users:
        if user is None:
            continue
        send_to_group(
            user_group(user.pk),
            {'type': 'conversation.removed', 'conversation_id': conversation.pk},
        )


def _schedule_sms_fanout(message):
    """Hand a new message to the SMS fan-out, if the app is installed.

    A function-local import, which is the house style for the one direction
    `chat` must not depend on at module level: `sms` imports `chat`, so the
    reverse at import time would be a cycle.
    """
    try:
        from sms.outbound import schedule
    except ImportError:  # pragma: no cover - sms is always installed today
        return
    schedule(message)


def broadcast_join_request(conversation, participant):
    """Tell the admins of a group that somebody is waiting to be let in.

    Over the websocket, to the admins only. A pending request is invisible
    otherwise - it sits in a queue nobody is told about, and the person who
    asked waits on an admin who does not know they asked.

    Not by SMS: an admin may be in many groups, every text is a real charge, and
    the app is where approving actually happens.
    """
    from chat.serializers import ParticipantSerializer

    payload = ParticipantSerializer(participant).data
    admins = Participant.objects.filter(
        conversation=conversation,
        status=Participant.Status.ACTIVE,
        role=Participant.Role.ADMIN,
        user__isnull=False,
    ).select_related('user')

    for admin in admins:
        send_to_group(
            user_group(admin.user_id),
            {
                'type': 'conversation.request',
                'conversation_id': conversation.pk,
                'participant': payload,
            },
        )
