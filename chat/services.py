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
                   source=Message.Source.APP, participant=None, client_id='',
                   reply_to=None, forwarded_from=None, copy_attachments=None):
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
    _refuse_if_blocked(conversation, sender)

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
                # Callers are responsible for proving the parent belongs to
                # this conversation - MessageCreateSerializer.validate_reply_to
                # and the consumer both do. Left unvalidated here so the single
                # write path does not grow a second copy of that query.
                reply_to=reply_to,
                # Provenance only. The API exposes a bare "forwarded" flag and
                # never who from: a forward crosses conversations, so naming
                # the original sender would tell the new room about somebody
                # in a room they cannot see.
                forwarded_from=forwarded_from,
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

    # A forward carries its files. New ROWS pointing at the same stored object
    # rather than a re-upload: the bytes are identical, and copying them would
    # double the storage bill every time somebody passes a photo along.
    #
    # Safe because nothing in this app deletes the underlying file - removing
    # an Attachment row leaves the object in place - so the copy cannot be
    # hollowed out by someone tidying the original.
    for original in copy_attachments or []:
        Attachment.objects.create(
            message=message,
            file=original.file.name,
            kind=original.kind,
            original_name=original.original_name,
            content_type=original.content_type,
            size=original.size,
            width=original.width,
            height=original.height,
        )

    # Moves the conversation to the top of everyone's list. auto_now fills
    # in the value.
    conversation.save(update_fields=['updated_at'])

    # And back INTO the list, for anybody who had tidied it away. Archive
    # means "not now" rather than "never again", so activity restores it -
    # except for muted members, for whom being pulled back to the top of the
    # list on every post is exactly the interruption they turned off.
    from chat.membership import unarchive_for_new_message

    unarchive_for_new_message(conversation)

    # Only broadcast once the row is really committed - subscribers on other
    # workers would otherwise race the transaction and fetch nothing.
    transaction.on_commit(lambda: broadcast_message(message))

    # And out to anybody taking part on a channel outside the app. Scheduled,
    # never run inline: each channel is N provider calls, and none of it may
    # happen inside the transaction still holding this row - see
    # sms/outbound.py:schedule.
    _schedule_delivery(message)

    # A card for the first link, worked out after the commit and pushed to
    # everyone as an update - see chat/link_previews.py.
    from chat.link_previews import schedule_link_preview

    schedule_link_preview(message)
    return message


def _refuse_if_blocked(conversation, sender):
    """A direct conversation between blocked people takes no new messages.

    Read-only rather than gone. The thread stays in both lists, with its
    history, because deleting somebody's own record of a conversation they had
    is a bigger thing than either person asked for - and because a thread that
    vanished from BOTH sides would tell the blocked person exactly what had
    happened.

    Checked here because this is the single write path for every route - REST,
    websocket, inbound SMS, inbound email - so there is one place to get it
    right rather than four. Groups are not consulted at all: a block is about
    direct contact, and both people stay full members of everything they share.

    The copy differs by who is asking and that is the whole point. The blocker
    is told plainly, because they already know. The blocked person is told
    something indistinguishable from any other refusal, because §2 of the spec
    forbids revealing that a block exists - and both come back as the same 403,
    so the status code cannot be read as an answer either.
    """
    if conversation.kind != Conversation.Kind.DIRECT or sender is None:
        return

    other = (
        Participant.objects.filter(conversation=conversation, user__isnull=False)
        .exclude(user=sender)
        .select_related('user')
        .first()
    )
    if other is None:
        return

    from user.blocks import blocks_between, has_blocked

    if not blocks_between(sender, other.user):
        return

    if has_blocked(sender, other.user):
        raise PostingNotAllowed(
            'You blocked this person. Unblock them to send a message.'
        )
    raise PostingNotAllowed('Unable to send message.')


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


def broadcast_message_updated(message):
    """An existing message changed - edited, deleted, or reacted to.

    ONE event for all three, carrying the whole message rather than a diff.
    Clients already know how to read a message and swap it into the thread by
    id, so three narrower events would be three more shapes to learn and three
    more consumer methods to forget - and a missing consumer method drops the
    frame in silence, which presents as "works after a refresh, never live".

    Deliberately not `message.new`: a client that treats this as new appends a
    duplicate instead of replacing the one it already has.
    """
    send_to_group(
        conversation_group(message.conversation_id),
        {'type': 'message.updated', 'message': serialize_message(message)},
    )


def announce_added_to_conversation(conversation, users):
    """Tell people somebody else put them in a conversation.

    Two things: an Activity notification, and their sockets joining it - see
    `subscribe_to_conversation` for the second. For somebody who STARTED the
    conversation, call that one alone: they are not told they were added to
    something they did themselves.
    """
    users = [user for user in users if user is not None]
    if not users:
        return

    from notification.models import Notification
    from notification.services import notify_many

    notify_many(users, Notification.Type.MEMBER_ADDED, conversation=conversation)
    subscribe_to_conversation(conversation, users)


def subscribe_to_conversation(conversation, users):
    """Make these people's open sockets join a conversation, after commit.

    A socket joins a conversation only when it connects or when it hears
    `conversation.added`. Before this existed only the people ADDED were ever
    sent that event - never whoever started the conversation - so the creator of
    a group or the starter of a direct message had a socket that was not in it.
    Their own messages still appeared, because the REST response carries them,
    which is how it went unnoticed; the other side's replies, typing and read
    receipts did not arrive until they reconnected.

    Published after commit, and the payload built from a fresh read, for two
    reasons that only show on a real deployment. Publishing inside the
    transaction lets a socket on another worker receive the event, subscribe,
    and read a conversation that has not been committed yet - the hazard
    `create_message` documents for message broadcasts. And serialising
    `conversation` as the caller holds it renders a participant prefetch taken
    BEFORE the new people existed, so each would be told a member list that
    does not include them.
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


def broadcast_conversation_updated(conversation):
    """Tell everyone in a conversation that its settings changed.

    Sent to the CONVERSATION's group, not to each member individually: everyone
    who can see it is already subscribed, and they all need the same payload.

    This matters more than a cosmetic refresh. `mode` decides which list a
    client files the group under and whether it offers a compose box at all, and
    `join_mode` decides how people get in. Without this event only the admin who
    made the change sees it; every other member keeps a stale group - filed in
    the wrong place, with a compose box that will be refused - until they happen
    to reload.
    """
    from chat.serializers import ConversationSerializer

    send_to_group(
        conversation_group(conversation.pk),
        {
            'type': 'conversation.updated',
            'conversation_id': conversation.pk,
            'conversation': ConversationSerializer(conversation).data,
        },
    )


def broadcast_conversation_read(conversation, user, message):
    """Say that this member has read up to here.

    Two audiences, and they are told for different reasons:

    Their OWN devices, so a badge cleared on the laptop clears on the phone.
    That frame carries no user id because it is about the recipient.

    The CONVERSATION, so other members can show a sender that their message
    has been seen. This half is a read receipt, and it is a deliberate product
    choice rather than a side effect: read state is only private until somebody
    asks to see it.
    """
    send_to_group(
        user_group(user.pk),
        {
            'type': 'conversation.read',
            'conversation_id': conversation.pk,
            'last_read_message_id': message.pk,
        },
    )
    send_to_group(
        conversation_group(conversation.pk),
        {
            'type': 'conversation.read.by',
            'conversation_id': conversation.pk,
            'user_id': user.pk,
            'last_read_message_id': message.pk,
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


def _schedule_delivery(message):
    """Hand a new message to every delivery channel that is installed.

    This used to name `sms.outbound.schedule` directly. It does not any more,
    because the second channel would have added a second import and a second
    call here - and the third a third, on the single code path every message in
    the product goes through. Channels register themselves instead; see
    `chat/delivery.py:register_channel`.

    A function-local import, which is the house style for the one direction
    `chat` must not depend on at module level.
    """
    from chat import delivery

    delivery.schedule_all(message)


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
