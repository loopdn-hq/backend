"""Serializers for conversations, membership and messages."""
import logging

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email as validate_email_address
from django.db import transaction
from rest_framework import serializers

from chat import membership
from chat.models import (
    Attachment,
    BlockedFromConversation,
    ChannelConsent,
    Conversation,
    GroupCategory,
    Message,
    MessageReaction,
    Participant,
    REACTION_EMOJI,
)
from chat.services import (
    announce_added_to_conversation,
    broadcast_conversation_updated,
    create_message,
    subscribe_to_conversation,
)
from user import privacy
from user.blocks import blocks_email, has_blocked
from user.lookups import find_user
from user.models import phone_number_validator
from user.serializers import PublicUserSerializer
from utils import regions

logger = logging.getLogger(__name__)


class AttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = [
            'id',
            'kind',
            'url',
            'original_name',
            'content_type',
            'size',
            'width',
            'height',
        ]
        read_only_fields = fields

    def get_url(self, obj):
        request = self.context.get('request')
        # Absolute when a request is available. Broadcasts have none, so
        # those carry the relative path.
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url


# Long enough to recognise the message being answered, short enough that a
# reply to a wall of text does not carry the wall of text with it.
REPLY_EXCERPT_CHARS = 120

# Everything MessageSerializer reads beyond the message row itself.
#
# Declared HERE, next to the serializer, and not spelled out at each call site:
# a nested field added to the serializer without the join that feeds it is an
# N+1 on the hottest page in the app, and nothing about the serializer shows it.
# Callers spread these instead, so adding a field fixes every queryset at once.
MESSAGE_SELECT_RELATED = (
    'sender__profile',
    # Messages that arrived by SMS have no `sender`, and the serializer falls
    # back to the membership row for the name.
    'sender_participant__user__profile',
    # The quoted parent, and whoever wrote it. `sender_label` reads the sender
    # OR the membership row, so both sides need joining.
    'reply_to__sender',
    'reply_to__sender_participant__user__profile',
)
MESSAGE_PREFETCH_RELATED = (
    'attachments',
    'reactions__user',
    # Who the message names. The profile is joined because a mention renders as
    # a display label, which for an account holder lives on the profile.
    'mentions__participant__user__profile',
)


# What a deleted message says in its place. One string in one place, so the
# thread, the reply excerpt and the conversation preview cannot drift apart.
DELETED_PLACEHOLDER = 'This message was deleted'


class MessageSerializer(serializers.ModelSerializer):
    """One message, as every client reads it.

    A DELETED message still appears - the row survives so the thread does not
    renumber around a hole and so replies quoting it keep their place - but its
    text and its files are withheld HERE rather than at each call site. This is
    the only way a message reaches a client, so withholding here is what makes
    "deleted" mean deleted.
    """

    sender = serializers.SerializerMethodField()
    text = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    reply_to = serializers.SerializerMethodField()
    reactions = serializers.SerializerMethodField()
    mentions = serializers.SerializerMethodField()
    forwarded = serializers.SerializerMethodField()
    link_preview = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id',
            'conversation',
            'sender',
            'text',
            'source',
            # Echoed so a sender can match this against the optimistic entry it
            # already rendered, instead of showing the message twice. Empty for
            # anything the client did not key - inbound SMS, older clients.
            'client_id',
            'attachments',
            # An EXCERPT of the message being answered, never the message
            # itself: nesting the real object recurses through a chain of
            # replies and carries its attachments with it.
            'reply_to',
            # One entry per emoji used, with who used it - not raw rows. A
            # busy message is a handful of pills, and sending the rows would
            # make every client group them itself, differently.
            'reactions',
            # Who the message names, and whether it named the whole group.
            # The client highlights them in the text it already has; the words
            # themselves carry no markup - see chat/mentions.py.
            'mentions',
            'mentions_everyone',
            # A bare flag, never who it came from. See create_message.
            'forwarded',
            'link_preview',
            # Null until somebody edits it, so "edited" is exactly
            # `edited_at is not None`. `updated_at` cannot answer this: it is
            # auto_now on a row nothing re-saves, so it equals created_at
            # forever.
            'edited_at',
            'deleted_at',
            'created_at',
        ]
        read_only_fields = fields

    def get_text(self, obj):
        return '' if obj.is_deleted else obj.text

    def get_attachments(self, obj):
        if obj.is_deleted:
            return []
        return AttachmentSerializer(
            obj.attachments.all(), many=True, context=self.context
        ).data

    def get_link_preview(self, obj):
        # Withheld with the text: a deleted message's link is part of what was
        # deleted.
        return None if obj.is_deleted else obj.link_preview

    def get_forwarded(self, obj):
        return obj.forwarded_from_id is not None

    def get_reactions(self, obj):
        """Who reacted with what, grouped by emoji.

        USERNAMES rather than a `reacted` boolean, because this payload is also
        what the broadcast carries and a broadcast has no request - there is no
        "me" to compute it against. Each client compares the list with its own
        username, which it already holds, and the same payload then serves the
        REST reader, the socket, and a future "who reacted" tooltip.

        Reads the prefetch. Without `prefetch_related('reactions__user')` on
        the queryset this is two queries per message on the history page.
        """
        if obj.is_deleted:
            return []

        grouped = {}
        for reaction in obj.reactions.all():
            grouped.setdefault(reaction.emoji, []).append(reaction.user.username)

        return [
            {'emoji': emoji, 'count': len(usernames), 'usernames': usernames}
            for emoji, usernames in grouped.items()
        ]

    def get_mentions(self, obj):
        """Who this message names: the membership, the account, the label.

        `participant_id` is what a client matches against its own member list,
        `username` is how a reader recognises THEMSELVES in it - usernames
        rather than a `mentions_me` flag, for the same reason `reactions`
        carries them: this payload is also what the broadcast sends, and a
        broadcast has no request and so no "me" to compute against.
        `display_label` is the name to highlight in the text.

        Withheld from a deleted message along with the text: the mention is
        part of what was deleted, and leaving it would keep a highlight under a
        message whose words are gone.

        Reads the prefetch - without `mentions__participant__user__profile` on
        the queryset this is two queries per message.
        """
        if obj.is_deleted:
            return []

        return [
            {
                'participant_id': row.participant_id,
                'user_id': row.participant.user_id,
                # Null for a phone-only member, who is reading nothing.
                'username': (
                    row.participant.user.username
                    if row.participant.user_id is not None
                    else None
                ),
                'display_label': row.participant.display_label,
            }
            for row in obj.mentions.all()
        ]

    def get_reply_to(self, obj):
        """The message this one answers, as much of it as a quote needs.

        None when it answers nothing, and also when the parent has since been
        deleted - `reply_to` is SET_NULL, so a reply outlives its parent and
        simply stops quoting it rather than disappearing itself.
        """
        parent = obj.reply_to
        if parent is None:
            return None

        if parent.is_deleted:
            # The quote goes when the original goes. Leaving the excerpt would
            # keep a copy of a deleted message visible under every reply to it.
            return {
                'id': parent.pk,
                'sender': parent.sender_label,
                'text': DELETED_PLACEHOLDER,
            }

        text = parent.text or ''
        return {
            'id': parent.pk,
            'sender': parent.sender_label,
            'text': (
                text
                if len(text) <= REPLY_EXCERPT_CHARS
                else text[:REPLY_EXCERPT_CHARS].rstrip() + '\u2026'
            ),
        }

    def get_sender(self, obj):
        """Who sent it - always the same shape, account or not.

        A message texted in by somebody with no LoopD account has no `sender`
        row, but every client reads `sender.profile.display_label` to render a
        line of chat. Emitting null would break all of them, so the keys stay
        put and only the ones that genuinely need an account come back null.
        """
        if obj.sender_id is not None:
            return PublicUserSerializer(obj.sender, context=self.context).data

        participant = obj.sender_participant
        return {
            'id': None,
            'username': None,
            'profile': {
                'display_label': participant.display_label if participant else 'Unknown',
                'avatar_url': None,
                'bio': '',
                'is_online': False,
                'last_seen_at': None,
            },
        }


class MessageCreateSerializer(serializers.Serializer):
    """Text, attachments, or both - but not neither.

    Emoji and links need nothing special: they are characters in `text`.
    """

    text = serializers.CharField(required=False, allow_blank=True)
    # The same retry key the websocket send takes. A REST POST that times out
    # is exactly as ambiguous to a client as a dropped socket.
    client_id = serializers.CharField(
        required=False, allow_blank=True, max_length=64
    )
    attachments = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
    )
    # The message being answered. Validated against THIS conversation - see
    # validate_reply_to, which is the security-relevant half of the feature.
    reply_to = serializers.IntegerField(
        required=False, allow_null=True, write_only=True
    )
    # Who the sender picked out of the member list, as membership ids. Ids
    # rather than names because most members have no handle to parse - see
    # chat/mentions.py - and ids that do not belong here are dropped there.
    mentions = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        write_only=True,
    )
    mention_everyone = serializers.BooleanField(required=False, write_only=True)
    # Pass a message along to THIS conversation. Posted to the destination, not
    # to the source, so every check that already guards a normal send - the
    # queryset that proves membership, the broadcast-mode rule, the block list
    # - guards a forward too, with no second copy to keep in step.
    forward_from = serializers.IntegerField(
        required=False, allow_null=True, write_only=True
    )

    def validate_reply_to(self, value):
        """Refuse a parent from any other conversation.

        Without this the id of a message in a group the sender is not in can be
        passed here, and its text comes straight back in the reply excerpt -
        turning "send a message" into a read primitive for private
        conversations. The conversation is taken from context rather than from
        the request, so a caller cannot widen it.
        """
        if value is None:
            return None

        conversation = self.context.get('conversation')
        if conversation is None:
            raise serializers.ValidationError(
                'Cannot resolve which conversation this reply belongs to.'
            )

        parent = Message.objects.filter(
            pk=value, conversation=conversation
        ).first()
        # And from before the replier was added with "Only show new messages":
        # the excerpt would hand them its text just the same.
        if parent is not None and not _sees(
            self.context.get('request'), conversation, parent
        ):
            parent = None
        if parent is None:
            # Deliberately not "no such message": distinguishing "does not
            # exist" from "exists somewhere you cannot see" is the oracle this
            # guard exists to close.
            raise serializers.ValidationError(
                'You can only reply to a message in this conversation.'
            )
        return parent

    def validate_forward_from(self, value):
        """Refuse anything the forwarder cannot already see.

        Read-across is the whole risk here: unchecked, this endpoint copies the
        text of any message in the database into a conversation the caller
        controls. So the source must be a message in a conversation they are an
        ACTIVE participant of - not merely one they were once in, and not one
        they are waiting to be approved for.
        """
        if value is None:
            return None

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            raise serializers.ValidationError('Sign in to forward a message.')

        original = (
            Message.objects.filter(
                pk=value,
                conversation__participants__user=user,
                conversation__participants__status=Participant.Status.ACTIVE,
            )
            .prefetch_related('attachments')
            .first()
        )
        # A message from before they were added with "Only show new messages"
        # is out of reach too - a forward copies its words and files.
        if original is not None and not _sees(
            request, original.conversation, original
        ):
            original = None
        if original is None:
            # Same refusal whether it does not exist or exists out of reach.
            raise serializers.ValidationError(
                'You can only forward a message you can see.'
            )
        if original.is_deleted:
            raise serializers.ValidationError(
                'That message was deleted and cannot be forwarded.'
            )
        return original

    def validate_attachments(self, files):
        if len(files) > settings.MAX_ATTACHMENTS_PER_MESSAGE:
            raise serializers.ValidationError(
                f'At most {settings.MAX_ATTACHMENTS_PER_MESSAGE} files per message.'
            )

        limit_mb = settings.MAX_ATTACHMENT_UPLOAD_BYTES // (1024 * 1024)
        for upload in files:
            if upload.size > settings.MAX_ATTACHMENT_UPLOAD_BYTES:
                raise serializers.ValidationError(
                    f'"{upload.name}" is too large. Keep files under {limit_mb}MB.'
                )
        return files

    def validate(self, attrs):
        # A forward carries the original's words, so it satisfies the
        # something-to-send rule by itself.
        if attrs.get('forward_from') is not None:
            return attrs
        if not (attrs.get('text') or '').strip() and not attrs.get('attachments'):
            raise serializers.ValidationError(
                'Send some text or at least one attachment.'
            )
        return attrs

    def create(self, validated_data):
        original = validated_data.get('forward_from')

        return create_message(
            conversation=validated_data['conversation'],
            sender=validated_data['sender'],
            # The forwarded words are taken from the original, never from the
            # request: a client that could supply its own text here would be
            # able to put words in a "Forwarded" frame that were never said.
            text=(
                original.text
                if original is not None
                else validated_data.get('text', '')
            ),
            uploads=validated_data.get('attachments'),
            client_id=validated_data.get('client_id', ''),
            reply_to=validated_data.get('reply_to'),
            forwarded_from=original,
            copy_attachments=(
                original.attachments.all() if original is not None else None
            ),
            # A forward carries the original's words but not its mentions: the
            # people it named are in the conversation it came from, and this is
            # a different room.
            mentions=(
                validated_data.get('mentions') if original is None else None
            ),
            mention_everyone=(
                validated_data.get('mention_everyone', False)
                if original is None
                else False
            ),
        )


class MessageEditSerializer(serializers.Serializer):
    """New words for a message you already sent.

    Text only. Editing the files would mean a message whose attachments no
    longer match the copy already texted or emailed out, and there is no way to
    correct those.
    """

    text = serializers.CharField(allow_blank=False, trim_whitespace=True)
    # An edit re-states who the message names, for the same reason it re-states
    # the words: taking "@Bob" out of the sentence has to take Bob out of the
    # mentions, or he keeps a highlight on a message that no longer names him.
    mentions = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        write_only=True,
    )
    mention_everyone = serializers.BooleanField(required=False, write_only=True)


class MessageReactionSerializer(serializers.Serializer):
    """One emoji, from the fixed set.

    A ChoiceField rather than a free string: the value is rendered in everyone
    else's thread, so an open field would let any member put 32 arbitrary
    characters under a message for the whole group to look at.
    """

    emoji = serializers.ChoiceField(choices=[(e, e) for e in REACTION_EMOJI])


def _reachable(participant, channel):
    """Whether this member has anywhere to receive `channel` at all.

    Only a member without an account is ruled out: they were added at one
    number or one address, and that is all there is. An account holder is left
    to their own answer, which they gave in settings.
    """
    if participant.is_phone_only:
        return channel != ChannelConsent.Channel.EMAIL
    if participant.is_email_only:
        return channel == ChannelConsent.Channel.EMAIL
    return True


class ParticipantSerializer(serializers.ModelSerializer):
    # Null for a member who has no account. `display_label` is the field a
    # client should render: it works for both kinds and needs no branching.
    user = PublicUserSerializer(read_only=True)
    display_label = serializers.CharField(read_only=True)
    is_phone_only = serializers.BooleanField(read_only=True)
    email_enabled = serializers.SerializerMethodField()
    channels = serializers.SerializerMethodField()

    class Meta:
        model = Participant
        fields = [
            'id',
            'user',
            'display_label',
            'is_phone_only',
            'role',
            'status',
            'muted',
            'sms_enabled',
            'email_enabled',
            # The full per-channel consent record. `sms_enabled` and
            # `email_enabled` above are the two booleans a toggle needs;
            # this is what actually happened, which is a different question
            # - "off" and "never answered" look identical as a boolean and
            # are very different things to show an admin.
            'channels',
            # How far this member has read, so a client can show a sender
            # whether their message has been seen. Null for anybody who has
            # read nothing - and permanently null for a phone-only member,
            # because SMS can report that a handset accepted a text and never
            # that a person read it. A receipt must not count them.
            'last_read_message',
            'joined_at',
        ]
        read_only_fields = fields

    def get_email_enabled(self, obj):
        """Whether this member currently takes this group by email.

        Derived rather than stored. Email's answer is a ChannelConsent row and
        not a column, because consent is per channel and a column per channel
        would mean a migration every time one is added. A client toggling it
        posts to `delivery/` exactly as it does for the other two switches, so
        the asymmetry stops here rather than reaching the API.

        Reads the prefetch when there is one - the same reader the fan-out uses.
        """
        from chat.delivery import consent_for

        consent = consent_for(obj, ChannelConsent.Channel.EMAIL)
        return bool(consent and consent.allows_delivery)

    def get_channels(self, obj):
        """Where this member stands on every channel, keyed by channel.

        Built by walking ChannelConsent.Channel rather than naming sms and
        email, so a channel added to the enum appears here without anyone
        remembering to widen this method.

        A channel the member was never asked about reports status None rather
        than being omitted: "we have not asked" is a real state an admin screen
        needs to distinguish from "they said no", and a missing key reads as
        neither.

        So does a channel a member without an account cannot be reached on,
        whatever its row says. Every such member is seeded a PENDING row per
        channel, and the 0028 backfill copied their invitation date onto all of
        them - which had somebody added by phone reading "Email pending" for an
        address that does not exist.

        Reads the prefetch - see the queryset in ConversationViewSet.participants,
        without which this is one query per channel per member.
        """
        from chat.delivery import consent_for

        record = {}
        for channel, _label in ChannelConsent.Channel.choices:
            consent = (
                consent_for(obj, channel) if _reachable(obj, channel) else None
            )
            record[channel] = {
                'status': consent.status if consent else None,
                # The decision the fan-out actually makes, so a client does not
                # have to re-derive it from status and get it subtly wrong.
                'allows_delivery': bool(consent and consent.allows_delivery),
                'asked_at': consent.asked_at if consent else None,
                'consented_at': consent.consented_at if consent else None,
                'opted_out_at': consent.opted_out_at if consent else None,
                # Whether to offer a nudge here at all, for the same reason.
                # The once-a-day floor is left to the endpoint: its refusal
                # says how long to wait, which a missing entry could not.
                'can_nudge': membership.standing_nudge_refusal(
                    obj, channel, consent
                ) is None,
            }
        return record

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get('last_read_message') is not None and not self._receipts_shown(
            instance
        ):
            data['last_read_message'] = None
        return data

    def _receipts_shown(self, participant):
        """Whether this member's read position may be shown to the viewer.

        Settings → Privacy → Read receipts, and it is a two-way bargain: with
        theirs off nobody sees their reads, and with the VIEWER's off they see
        nobody's. A viewer always sees their own row, which is what clears
        their own badge rather than a receipt about anybody.
        """
        viewer = getattr(self.context.get('request'), 'user', None)
        if participant.user_id is None or (
            viewer is not None and viewer.pk == participant.user_id
        ):
            return True

        if not self._viewer_shows_receipts(viewer):
            return False
        return privacy.shows_read_receipts(participant.user)

    def _viewer_shows_receipts(self, viewer):
        # Cached on the serializer, which is shared across a page of members,
        # so a list of twenty costs one lookup rather than twenty.
        if not hasattr(self, '_viewer_receipts'):
            self._viewer_receipts = privacy.shows_read_receipts(viewer)
        return self._viewer_receipts


class GroupSettingsFieldsSerializer(serializers.Serializer):
    """How a group behaves - its policy, as opposed to what it is called.

    One serializer used by BOTH create and update, so there is a single shape to
    learn rather than two that drift apart. Every field optional; anything left
    out keeps its default.

    `name`, `description` and `image` are deliberately NOT here. They are the
    group's identity, they are what `edit_group_permission` can be relaxed to
    let ordinary members change, and the policy below must not travel with them.
    """

    mode = serializers.ChoiceField(choices=Conversation.Mode.choices, required=False)
    join_mode = serializers.ChoiceField(
        choices=Conversation.JoinMode.choices, required=False
    )
    add_member_permission = serializers.ChoiceField(
        choices=Conversation.MemberAddPermission.choices, required=False
    )
    edit_group_permission = serializers.ChoiceField(
        choices=Conversation.EditGroupPermission.choices, required=False
    )
    sms_delivery_enabled = serializers.BooleanField(required=False)
    sms_replies_enabled = serializers.BooleanField(required=False)
    voice_messages_enabled = serializers.BooleanField(required=False)
    media_enabled = serializers.BooleanField(required=False)
    reactions_enabled = serializers.BooleanField(required=False)

    # What kind of group this is and where it is - the only two facts about a
    # group advertising may target on (Conversation.category). A category's
    # slug, or null to say nothing; a retired category cannot be chosen.
    category = serializers.SlugRelatedField(
        slug_field='slug',
        queryset=GroupCategory.objects.filter(active=True),
        required=False,
        allow_null=True,
    )
    # A country ("PH") or one of its regions ("PH-00"), from utils/regions.py;
    # blank to say nothing.
    region = serializers.CharField(max_length=6, required=False, allow_blank=True)

    def validate_region(self, value):
        value = value.strip().upper()
        if value and not regions.is_region(value):
            raise serializers.ValidationError(f'"{value}" is not a region we know.')
        return value


#: Changing any of these is an admin decision even in a group where members may
#: edit the name and photo. A member who could grant themselves permissions
#: would make `edit_group_permission` meaningless.
ADMIN_ONLY_SETTINGS = {
    'mode',
    'join_mode',
    'add_member_permission',
    'edit_group_permission',
    'sms_delivery_enabled',
    'sms_replies_enabled',
    'voice_messages_enabled',
    'media_enabled',
    'reactions_enabled',
    # Which ads the group can carry follows from these two.
    'category',
    'region',
}


class GroupNumberSerializer(serializers.Serializer):
    """The group's own phone number, as a client needs to see it.

    A bare string was enough while the only question was "what do I dial". It
    cannot answer "can I dial it yet": a group whose provisioning has not
    succeeded has no number at all, so the whole object is null and a client
    has to handle that rather than render an empty string. `provider` names who
    the lease is with, which is what tells a support conversation where to go
    looking; it is the one field here that is not decoration.

    Nothing ever fabricates a stand-in number, so a number present in this
    payload is always a real one somebody can text.

    A plain Serializer, not a ModelSerializer, so this module stays free of any
    import of `sms` - `sms.models` imports `chat.models`, and chat's side of
    that relationship is deliberately kept to function-local imports.

    `provider_number_id` is deliberately absent: it is the lease's
    account-management handle, used to reconfigure or release the number, and
    no member of the group has any use for it.
    """

    number = serializers.CharField(read_only=True)
    country = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    provider = serializers.CharField(read_only=True)


class ConversationSerializer(serializers.ModelSerializer):
    # No `participants`. The member list is unbounded and every row carries a
    # nested user and profile, so a group of two hundred made every fetch of
    # every conversation carry two hundred profiles - on the list endpoint,
    # once per conversation. It lives on its own paginated endpoint instead:
    # GET /conversations/<id>/participants/.
    #
    # The participants PREFETCH in ConversationViewSet.get_queryset stays even
    # so: `get_title` walks it to name a direct conversation after the other
    # person, and without it that is a query per row.
    title = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    settings = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    is_admin = serializers.SerializerMethodField()
    # What the viewer may do IN THIS conversation, for driving the UI. Inline
    # rather than a second endpoint: the list already carries everything else
    # a client needs, and a round trip per group would cost more than the
    # boolean it replaces. `is_admin` stays beside it until the last client
    # call site has moved - see docs/rbac_plan.md step 6.
    features = serializers.SerializerMethodField()
    admin_details = serializers.SerializerMethodField()
    # The number members text to reach this group. Read-only everywhere and
    # for everybody: it is fixed for the life of the group, so there is no
    # writable form of this field for a client or an admin to reach.
    sms_number = serializers.SerializerMethodField()
    # Nested to match what POST .../group/ and PATCH .../settings/ already
    # accept (GroupSettingsFieldsSerializer). source='*' hands the whole
    # Conversation instance through, since these are plain model fields
    # rather than a related object - this endpoint used to leave `settings`
    # out entirely, so the client's Admin Panel Settings tab always saw null.
    settings = GroupSettingsFieldsSerializer(source='*', read_only=True)
    unread_count = serializers.SerializerMethodField()
    muted = serializers.SerializerMethodField()
    archived = serializers.SerializerMethodField()
    cleared = serializers.SerializerMethodField()
    pinned_message = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id',
            'kind',
            'name',
            'title',
            'description',
            'image_url',
            # One object, the same shape a client PATCHes to /settings/, so
            # there is one thing to learn rather than a nested request and a
            # flat response. `mode` and `join_mode` used to sit out here too
            # and no longer do - they are inside `settings` with the other five.
            'settings',
            'created_by',
            'is_admin',
            'features',
            'admin_details',
            'sms_number',
            'last_message',
            # The one message held above the thread, or null. On the
            # conversation rather than on a message so pinning reuses the
            # `conversation.updated` broadcast every client already handles.
            'pinned_message',
            'unread_count',
            'muted',
            'archived',
            'cleared',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def _own_participation(self, obj):
        """The caller's own membership row, or None.

        Read off the prefetch the list queryset already carries rather than
        querying per conversation - the same list `is_admin` walks. Falls back
        to nothing for an anonymous or non-member caller, who cannot reach
        these routes anyway.
        """
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        for participant in obj.participants.all():
            if participant.user_id == user.pk:
                return participant
        return None

    def get_muted(self, obj):
        """Whether this member has silenced the group, on every channel.

        Their own state, never an admin's: the same flag texting MUTE sets, and
        the one the SMS fan-out checks before sending.
        """
        participant = self._own_participation(obj)
        return bool(participant and participant.muted)

    def get_archived(self, obj):
        participant = self._own_participation(obj)
        return bool(participant and participant.archived_at)

    def get_cleared(self, obj):
        """True when this member has nothing left to see in here.

        They cleared their view and nothing has arrived since, so a client
        should leave it out of the list entirely. It becomes false again the
        moment somebody posts, which is what brings the conversation back
        holding only the new messages - no second flag to keep in step.

        Read off the prefetched newest message rather than counting, so this
        costs nothing on a list of any length.

        Two markers, because one cannot answer both cases. The message id
        settles it whenever the conversation has messages. `cleared_at` exists
        for the conversation that had NONE when it was cleared: there was
        nothing to point at, so the id is still null and only the timestamp
        says the member ever cleared it.
        """
        participant = self._own_participation(obj)
        if participant is None:
            return False

        cleared_before = participant.cleared_before_message_id
        cleared_at = participant.cleared_at
        if cleared_before is None and cleared_at is None:
            return False

        latest = getattr(obj, 'latest_messages', None)
        newest = latest[0] if latest else obj.messages.first()
        if newest is None:
            # Nothing in here at all: cleared exactly when they said so.
            return cleared_at is not None

        if cleared_before is not None and newest.pk <= cleared_before:
            return True
        # Cleared while empty, and still nothing since.
        return cleared_at is not None and newest.created_at <= cleared_at

    def get_unread_count(self, obj):
        """Messages past the caller's read pointer, not counting their own.

        Read off an annotation the list and detail querysets add, because
        computing it here would be a query per conversation on every list.

        Defaults to 0 rather than assuming the annotation is there: several
        actions re-serialize a conversation they fetched outside
        get_queryset - a freshly created group, the payload
        broadcast_conversation_updated puts on the socket - and those
        instances carry no annotation. A missing count is not "unread".
        """
        return getattr(obj, 'unread_count', 0)

    def get_sms_number(self, obj):
        """The group's number as an object, or None while it has not got one.

        Without this the product's central promise - text this number to reach
        your group - is unreachable: a member has no way to learn the number,
        so nobody can use SMS at all.

        Nested rather than a bare string because "the number exists" and "the
        number works" are different questions and only the object answers both;
        see GroupNumberSerializer.
        """
        if obj.kind != Conversation.Kind.GROUP:
            return None
        number = getattr(obj, 'sms_number', None)
        if number is None:
            return None
        return GroupNumberSerializer(number).data

    def get_settings(self, obj):
        """Every group setting, in one object.

        Read-only here. ADMIN_ONLY_SETTINGS governs who may CHANGE these, but
        every member may SEE them: a member who cannot tell whether SMS
        delivery is on cannot understand why their message reached nobody.

        `join_code` is deliberately absent. It is the thing that lets a
        stranger in, so it keeps its own admin-only endpoint rather than riding
        every conversation fetch into logs, caches and screenshots.
        """
        return {
            'mode': obj.mode,
            'join_mode': obj.join_mode,
            'add_member_permission': obj.add_member_permission,
            'edit_group_permission': obj.edit_group_permission,
            'sms_delivery_enabled': obj.sms_delivery_enabled,
            'sms_replies_enabled': obj.sms_replies_enabled,
            'voice_messages_enabled': obj.voice_messages_enabled,
            'media_enabled': obj.media_enabled,
            'reactions_enabled': obj.reactions_enabled,
            'category': obj.category.slug if obj.category_id else None,
            'region': obj.region,
        }

    def get_image_url(self, obj):
        """A group's image, or None. Direct conversations never have one.

        `image_url` rather than `image` for the same reason as the avatar on a
        profile: the bucket is private and django-storages signs each URL, so
        the stored path is no use to a client on its own.
        """
        if not obj.image:
            return None
        request = self.context.get('request')
        # Absolute when a request is available. Broadcasts have none, so
        # those carry the relative path - same as AttachmentSerializer.
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url

    def get_title(self, obj):
        """What to show in a conversation list.

        A group has a name. A direct conversation is named after whoever the
        viewer is talking to, so it depends on who is asking.
        """
        if obj.kind == Conversation.Kind.GROUP:
            return obj.name

        viewer = self._viewer()
        for participant in obj.participants.all():
            if participant.user_id is None:
                # An EXTERNAL thread is titled after exactly this row - the
                # address on the far side - and `display_label` already falls
                # back to the local part for it. A phone-only row is still
                # skipped: it cannot occur in a one-to-one, and titling one
                # after it would be a guess.
                if obj.is_external_direct and participant.email:
                    return participant.display_label
                continue
            if viewer is None or participant.user_id != viewer.pk:
                return participant.display_label
        return ''

    def get_last_message(self, obj):
        # Populated by the list view's Prefetch; falls back to a query for
        # the detail view, where one extra query costs nothing.
        prefetched = getattr(obj, 'latest_messages', None)
        message = prefetched[0] if prefetched else None
        if prefetched is None:
            message = obj.messages.first()
        if message is None or not self._viewer_sees(obj, message):
            return None
        return MessageSerializer(message, context=self.context).data

    def get_pinned_message(self, obj):
        """The pinned message in full, or null.

        A deleted message stops being pinned rather than showing a tombstone at
        the top of the thread - the pin is a place to keep something useful, and
        a placeholder there is just clutter nobody can clear but an admin.
        """
        message = obj.pinned_message
        if message is None or message.is_deleted:
            return None
        if not self._viewer_sees(obj, message):
            return None
        return MessageSerializer(message, context=self.context).data

    def _viewer_sees(self, obj, message):
        """False for a message from before the viewer was added with "Only
        show new messages" - the preview and the pin are history too.

        A payload with no viewer (a broadcast) keeps the message; each socket
        drops what its own account may not see - see ChatConsumer.
        """
        participant = self._own_participation(obj)
        return participant is None or participant.sees_message(message.pk)

    def _viewer(self):
        request = self.context.get('request')
        return getattr(request, 'user', None) if request else None

    def _person(self, user):
        """One person as {username, avatar}, or None.

        `avatar` carries the same absolute URL as `avatar_url` elsewhere in the
        API - built from the request so a mobile client does not have to know
        the host - and is None when they have not set one.
        """
        if user is None:
            return None
        profile = getattr(user, 'profile', None)
        avatar = getattr(profile, 'avatar', None) if profile else None
        url = None
        if avatar:
            request = self.context.get('request')
            url = request.build_absolute_uri(avatar.url) if request else avatar.url
        return {'username': user.username, 'avatar': url}

    def get_created_by(self, obj):
        """Whoever started this conversation."""
        return self._person(obj.created_by)

    def get_features(self, obj):
        """The viewer's feature keys for this conversation.

        Not access control - that is `get_queryset`, which is why an outsider
        never reaches this method at all. This says what to SHOW someone who is
        already in the room, and every write is re-checked server-side.

        The role comes from the SAME prefetched rows `get_is_admin` walks, and
        is handed to the resolver rather than looked up: this runs once per
        conversation on a page that lists twenty, and
        `test_the_new_fields_do_not_cost_a_query_per_conversation` is what
        holds the line.
        """
        from rbac.resolver import features_for_roles

        viewer = self._viewer()
        if viewer is None or not getattr(viewer, 'is_authenticated', False):
            return []

        roles = {
            row.role for row in obj.participants.all()
            if row.user_id == viewer.pk
        }
        keys = features_for_roles(roles, obj, 'conversation', user=viewer)

        # A one-to-one has no admins, so pinning there is a MEMBERSHIP question
        # rather than a role one - `Conversation.may_pin` answers it with an
        # early return before any role is consulted, and rbac/policy.py may
        # only narrow a grant, never widen one.
        #
        # This field has to say what the person can actually DO, or a client
        # reading it would hide the pin control on every direct message. The
        # special case lives here, next to the one place it is true, rather
        # than being smuggled into the role grants - where it would make the
        # permissions matrix claim members can pin, false in every group.
        if obj.kind == Conversation.Kind.DIRECT and roles:
            keys = keys | {'group.message.pin'}

        return sorted(keys)

    def get_is_admin(self, obj):
        """Whether the person asking administers this group.

        The creator counts automatically; anyone else has to hold the admin
        role on their membership row.

        NOTE the one case where this is more generous than the server: a
        creator who has been DEMOTED still reads True here, while
        `conversation.is_admin()` - which every admin-only endpoint actually
        enforces - reads False. A client trusting this flag would offer them
        controls the API then refuses with 403. It cannot happen to a creator
        who merely left, because a non-member cannot fetch the conversation at
        all, and it needs a second admin to exist before the first can be
        demoted at all.
        """
        viewer = self._viewer()
        if viewer is None or not getattr(viewer, 'is_authenticated', False):
            return False
        if obj.kind != Conversation.Kind.GROUP:
            # Nobody administers a one-to-one conversation.
            return False
        if obj.created_by_id == viewer.pk:
            return True
        return any(
            row.user_id == viewer.pk and row.role == Participant.Role.ADMIN
            for row in obj.participants.all()
        )

    def get_admin_details(self, obj):
        """Who administers this group, as {username, avatar}.

        The creator when they are asking, otherwise the admin found on the
        membership rows.

        A group can have SEVERAL admins - `set_role` promotes people - and this
        field is one object, so it has to choose. It prefers the creator when
        they still hold the role, then the earliest to have joined, which makes
        the answer stable between requests rather than dependent on row order.
        """
        if obj.kind != Conversation.Kind.GROUP:
            return None

        viewer = self._viewer()
        if (
            viewer is not None
            and getattr(viewer, 'is_authenticated', False)
            and obj.created_by_id == viewer.pk
        ):
            return self._person(obj.created_by)

        admins = [
            row for row in obj.participants.all()
            if row.role == Participant.Role.ADMIN and row.user_id is not None
        ]
        if not admins:
            return None
        admins.sort(key=lambda row: (row.user_id != obj.created_by_id, row.joined_at))
        return self._person(admins[0].user)


class ConversationSearchSerializer(serializers.ModelSerializer):
    """A group as somebody outside it sees it, before deciding to join.

    Deliberately thinner than ConversationSerializer: this is the one place a
    NON-MEMBER reads a group, so it carries what is needed to choose - the
    name, what it is for, how big it is and how to get in - and nothing that
    belongs to the people already inside. No messages, no participants, no
    sms_number, no join_code.
    """

    image_url = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id',
            'name',
            'description',
            'image_url',
            'member_count',
            'join_mode',
            'mode',
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        request = self.context.get('request')
        if not obj.image:
            return None
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url

    def get_member_count(self, obj):
        """Active members only, and annotated by the view so this is free.

        Falls back to a query rather than raising when the annotation is
        missing, so the serializer stays usable from a shell or a test.
        """
        annotated = getattr(obj, 'active_member_count', None)
        if annotated is not None:
            return annotated
        return obj.active_participants.count()


class DirectConversationSerializer(serializers.Serializer):
    """Start (or find) a one-to-one conversation with somebody."""

    identifier = serializers.CharField(
        help_text=(
            'A username, a phone number in +E.164 form, or an email address.'
        )
    )

    #: What the blocker is told. Only ever shown to the person who made the
    #: block - see `validate_identifier`.
    BLOCKED_BY_ME = 'You blocked this user. Unblock to message them.'

    #: What everybody else is told, blocked or simply mistyped.
    NOT_FOUND = 'No user found with that username or number.'

    #: Set by `validate_identifier` when the identifier is an address nobody
    #: has. `create` reads it to open an external thread rather than refusing.
    external_address = None

    #: What somebody is told when the other person's Privacy settings refuse.
    #: Plain, unlike the block wording above: a privacy setting is not a secret.
    REFUSED = privacy.DIRECT_MESSAGES_REFUSED

    def validate_identifier(self, value):
        viewer = self.context['request'].user
        other = find_user(value, exclude=viewer)
        if other is not None:
            # "Who can send me direct messages", from Settings → Privacy. Only
            # STARTING one is refused: a thread they already have carries on,
            # because stranding a conversation somebody is in is what blocking
            # is for, not a setting about who may open a new one.
            if not privacy.allows_direct_message(other, viewer) and not (
                Conversation.objects.filter(
                    direct_key=Conversation.direct_key_for(viewer, other)
                ).exists()
            ):
                raise serializers.ValidationError(self.REFUSED)
            self.other = other
            return value

        # Nobody resolved. Either the name is wrong, or `find_user` filtered
        # out somebody in a block relationship with the viewer - and those two
        # people must be told different things.
        #
        # THE BLOCKER IS TOLD PLAINLY. They already know they blocked somebody,
        # so hiding it buys no privacy and only leaves them at a dead end that
        # looks like a bug. THE BLOCKED PERSON IS TOLD NOTHING: for them the
        # answer must be the one a stranger's typo gets, or the refusal itself
        # announces the block.
        #
        # Which is why this asks `has_blocked` - one direction - rather than
        # `blocks_between`. It is the only place in the product where the two
        # sides of a block are deliberately treated differently.
        candidate = find_user(value, exclude=viewer, apply_blocks=False)
        if candidate is not None and has_blocked(viewer, candidate):
            raise serializers.ValidationError(self.BLOCKED_BY_ME)

        # Nobody has it - and if it is an address, that is not a dead end any
        # more. AFTER the two branches above, deliberately: an address whose
        # owner hid it in Settings → Privacy, or who is on the far side of a
        # block, is answered by them and never reaches here, so this cannot
        # become the account lookup `user/invites.py` exists to refuse.
        #
        # `find_user` treats '@' as a TRY and falls through to the username
        # branch, because Django allows '@' in a username - so a real address
        # is the thing that is left once BOTH have missed, and it still has to
        # validate. Without that check a mistyped username containing '@'
        # would open a bridge to a mailbox nobody owns.
        address = value.strip().lower()
        if '@' in address:
            try:
                validate_email_address(address)
            except DjangoValidationError:
                raise serializers.ValidationError(self.NOT_FOUND)
            if address == (viewer.email or '').strip().lower():
                raise serializers.ValidationError(self.NOT_FOUND)
            if blocks_email(viewer, address):
                raise serializers.ValidationError(self.BLOCKED_BY_ME)
            self.external_address = address
            return value

        raise serializers.ValidationError(self.NOT_FOUND)

    def create(self, validated_data):
        starter = self.context['request'].user

        address = getattr(self, 'external_address', None)
        if address is not None:
            from user.invites import TooManyInvites, consume_invite_allowance

            # The same bucket `user/invites.py` counts against, not a second
            # one beside it: two doors onto one cap, or the real limit is
            # double what either says. Counted as an ATTEMPT, so a probe costs
            # the prober the same whether or not anything is sent.
            if not consume_invite_allowance(starter):
                raise TooManyInvites

            conversation, created = Conversation.start_direct_external(
                starter, address
            )
            if created:
                subscribe_to_conversation(conversation, [starter])
            return conversation

        conversation, created = Conversation.start_direct(starter, self.other)
        if created:
            announce_added_to_conversation(conversation, [self.other])
            # Not told they were added - they started it - but their socket
            # must still join, or the reply never reaches them live.
            subscribe_to_conversation(conversation, [starter])
        return conversation


class InviteEntrySerializer(serializers.Serializer):
    """One person to add by number who has no account yet."""

    phone_number = serializers.CharField(max_length=16)
    display_name = serializers.CharField(max_length=80)

    def validate_phone_number(self, value):
        number = value.strip()
        phone_number_validator(number)
        return number


class GroupCreateSerializer(serializers.Serializer):
    """Create a group. Whoever calls this becomes its admin.

    `description` and `image` are both optional, so a client that sends
    nothing but a name behaves exactly as it did before they existed. Sending
    an image makes the request multipart, in which `members` has to be
    repeated form keys rather than a JSON array.
    """

    name = serializers.CharField(max_length=120)
    description = serializers.CharField(
        max_length=280, required=False, allow_blank=True
    )
    # Write-only, like the attachments above and the avatar on a profile: the
    # client sends a file and gets `image_url` back in the response.
    image = serializers.ImageField(required=False, allow_null=True, write_only=True)
    members = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text='Usernames, emails or verified +E.164 numbers of EXISTING accounts.',
    )
    # Separate from `members`, not merged into it, because the two behave
    # differently when something is wrong: `members` means "these people exist,
    # fail if they do not", so a typo is caught. Merged, a mistyped username
    # would quietly become somebody's phone number.
    invite = InviteEntrySerializer(
        many=True,
        required=False,
        help_text='People with no account, added by number and texted an invitation.',
    )
    # Separate from `members` for the opposite reason `invite` is: an address
    # that resolves to nobody is not a typo to reject, it is somebody who has
    # not signed up yet. `members` fails the whole request on a miss; this one
    # emails them instead.
    invite_email = serializers.ListField(
        child=serializers.EmailField(),
        required=False,
        help_text='Email addresses. A verified account is added outright; '
                  'anyone else is emailed an invitation.',
    )
    settings = GroupSettingsFieldsSerializer(required=False)

    def validate_image(self, value):
        return validate_group_image(value)

    def validate_members(self, values):
        creator = self.context['request'].user
        members = resolve_members(values, exclude=creator)
        # "Who can add me to groups", from Settings → Privacy. All or none: an
        # admin told only that somebody refused cannot finish the job, so the
        # refusal names them and they can be taken off the list.
        refused = [
            user for user in members if not privacy.allows_group_add(user, creator)
        ]
        if refused:
            raise serializers.ValidationError(privacy.group_add_refusal(refused))
        return members

    def create(self, validated_data):
        creator = self.context['request'].user
        conversation = self._create_group(validated_data, creator)

        # Its own phone number, in the creator's country. Deliberately AFTER the
        # group is committed rather than inside `start_group`: ordering a number
        # is a call to somebody else's API, and a provider that is slow or down
        # must not turn "create a group" into "create nothing". Worst case the
        # group is app-only for a moment.
        from sms.provisioning import provision_for

        try:
            provision_for(conversation, creator)
        except Exception:
            logger.exception(
                'Could not provision a number for conversation %s', conversation.pk
            )

        # After provisioning, because an invitation is a text and a text needs
        # the group's number to come from.
        self._invite_by_number(
            conversation, validated_data.get('invite') or [], creator
        )
        self._invite_by_email(
            conversation, validated_data.get('invite_email') or [], creator
        )

        return conversation

    def _create_group(self, validated_data, creator):
        from billing.limits import check_can_create_group

        check_can_create_group(creator)

        conversation = Conversation.start_group(
            creator,
            validated_data['name'],
            description=validated_data.get('description', ''),
            image=validated_data.get('image'),
        )

        group_settings = validated_data.get('settings') or {}
        if group_settings:
            for field, value in group_settings.items():
                setattr(conversation, field, value)
            conversation.save(update_fields=list(group_settings))

        members = validated_data.get('members') or []
        for user in members:
            conversation.add_participant(user)
        if members:
            announce_added_to_conversation(conversation, members)

        # The creator's own socket, which nothing else puts in the group.
        subscribe_to_conversation(conversation, [creator])

        return conversation

    def _invite_by_email(self, conversation, addresses, creator):
        """Add or invite each address, after the group exists.

        Same shape as `_invite_by_number` and for the same reason: each is
        handled on its own and never allowed to fail the whole request. The
        group and its real members are already committed, and losing them
        because one address bounced helps nobody.
        """
        from chat import membership

        invited = []
        for address in addresses:
            try:
                # None when the address belongs to somebody who hid it in
                # Settings → Privacy: nothing added, nothing emailed.
                row = membership.invite_by_email(conversation, address, creator)
                if row is not None:
                    invited.append(row)
            except Exception:
                logger.exception(
                    'Could not invite %s to conversation %s',
                    address, conversation.pk,
                )
        return invited

    def _invite_by_number(self, conversation, entries, creator):
        """Add the people who have no account, after the group exists.

        Each is texted an invitation and receives nothing from the group until
        they reply YES. Done one at a time and never allowed to fail the whole
        request: the group and its real members are already committed, and
        losing them because one number was unreachable helps nobody.
        """
        from chat import membership

        invited = []
        for entry in entries:
            try:
                invited.append(membership.invite_by_phone(
                    conversation,
                    entry['phone_number'],
                    entry['display_name'],
                    creator,
                ))
            except Exception:
                logger.exception(
                    'Could not invite %s to conversation %s',
                    entry['phone_number'], conversation.pk,
                )
        return invited


class GroupSettingsSerializer(serializers.ModelSerializer):
    """Edit a group's name, description and image. Admins only.

    A ModelSerializer, unlike the create serializer above it, because this one
    really is "PATCH these three columns" - there are no identifiers to
    resolve into users. Every field is optional: a client changing nothing but
    the photo sends nothing but the photo.
    """

    # Declared rather than inherited from the model. The column is blank=True
    # because a direct conversation has no name, and a ModelSerializer would
    # copy that into allow_blank - letting a PATCH erase a group's name that
    # GroupCreateSerializer refuses to accept blank in the first place.
    name = serializers.CharField(max_length=120, required=False)
    # allow_null is what lets a group photo be removed again: multipart has no
    # null, so DRF reads an empty `image` field as None.
    image = serializers.ImageField(required=False, allow_null=True, write_only=True)

    # The same object `POST /group/` takes, so there is one shape to learn.
    settings = GroupSettingsFieldsSerializer(required=False)

    class Meta:
        model = Conversation
        fields = ['name', 'description', 'image', 'settings']

    def validate_image(self, value):
        return validate_group_image(value)

    def update(self, instance, validated_data):
        if not validated_data:
            return instance

        group_settings = validated_data.pop('settings', None) or {}
        for field, value in validated_data.items():
            setattr(instance, field, value)
        for field, value in group_settings.items():
            setattr(instance, field, value)

        fields = list(validated_data) + list(group_settings)

        # A group can predate join codes, or have had one revoked. Switching to
        # a mode that needs one and leaving it NULL would make the group
        # unjoinable while the setting says otherwise.
        if instance.join_mode == Conversation.JoinMode.CODE and not instance.join_code:
            instance.join_code = Conversation._unused_join_code()
            fields.append('join_code')

        # Named fields rather than a plain save(): `updated_at` is auto_now and
        # Meta.ordering is ['-updated_at'], so saving everything would shove
        # the group to the top of every member's conversation list because
        # somebody fixed a typo in the description. Only a message is activity.
        instance.save(update_fields=fields)

        # After commit, and only once the row is really written: every other
        # member is looking at a stale copy until this lands, and `mode` in
        # particular moves the group between lists in their sidebar.
        transaction.on_commit(
            lambda: broadcast_conversation_updated(instance)
        )
        return instance


class AddMembersSerializer(serializers.Serializer):
    """Add people to a group by username or phone number, or invite by email.

    `invite_email` is for addresses with no account behind them, and goes
    through `membership.invite_by_email` exactly as it does when a group is
    created: a row against the address, and an invitation that asks them to
    reply YES or sign up. An address that turns out to be an account's is
    added outright, and one whose owner hid it is silently skipped - see there.
    """

    members = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    # Capped, because every entry is an email to a stranger.
    invite_email = serializers.ListField(
        child=serializers.EmailField(), required=False, default=list,
        max_length=20,
    )
    # What the people being added may read: everything said before, or only
    # what is said from now on. The admin's choice, asked when they add
    # somebody; "all" is what every other way into a group gives.
    history = serializers.ChoiceField(
        choices=['all', 'new'], required=False, default='all',
    )

    def validate(self, attrs):
        if not attrs.get('members') and not attrs.get('invite_email'):
            raise serializers.ValidationError('Name at least one person to add.')
        return attrs

    def validate_members(self, values):
        # `exclude=None`, so `find_user` does NOT drop a blocked pair here.
        # That is on purpose and it is not the same shortcut the sibling
        # serializer takes: an admin adding several people needs each name to
        # resolve or fail on its own merits, and a lookup that silently found
        # nobody would be reported as "no such user" rather than as a refusal.
        # The block is enforced below instead, where it can actually refuse.
        resolved = resolve_members(values, exclude=None)

        # §11 of the blocking spec, on the route people actually use. This was
        # enforced on invite-by-email and invite-by-number while the ordinary
        # "add members" endpoint - the one the app calls - went straight to
        # `add_participant`, which knows only about the admin-imposed block and
        # nothing about one person blocking another.
        request = self.context.get('request')
        adder = getattr(request, 'user', None)
        for user in resolved:
            membership._refuse_blocked_invite(adder, user)

        # And their own "Who can add me to groups". Somebody already in the
        # group is exempt: re-adding them is a no-op, and refusing it would
        # make an admin's harmless repeat look like a rejection.
        conversation = self.context.get('conversation')
        refused = [
            user for user in resolved
            if not privacy.allows_group_add(user, adder)
            and not (conversation and conversation.has_participant(user))
        ]
        if refused:
            raise serializers.ValidationError(privacy.group_add_refusal(refused))

        return resolved

    @transaction.atomic
    def save(self, **kwargs):
        """All of them, or none.

        `add_participant` raises for anybody blocked, and without a transaction
        the people listed before them are already committed while the response
        is a 403 - so the admin is told it failed, half of it happened, and the
        broadcast that tells those people they were added never ran.
        """
        conversation = self.context['conversation']
        users = self.validated_data['members']
        addresses = sorted({
            address.strip().lower()
            for address in self.validated_data['invite_email']
        })

        # add_participant is idempotent, so re-adding an existing member is
        # a no-op rather than an error - but they should not be told they
        # were just added, either.
        added = [
            user for user in users if not conversation.has_participant(user)
        ]
        # Re-inviting an address already in the group re-sends its invitation
        # and adds nobody, so it does not count against the plan.
        already_invited = set(
            Participant.objects.filter(
                conversation=conversation,
                email__in=addresses,
                status=Participant.Status.ACTIVE,
            ).values_list('email', flat=True)
        )
        new_invites = [a for a in addresses if a not in already_invited]

        if added or new_invites:
            from billing.limits import check_can_add_members
            check_can_add_members(conversation, len(added) + len(new_invites))

        # Who is already in, so the history choice below lands only on the
        # people this request actually brings in. An address that turns out to
        # be a current member's, or one already invited, must keep what they
        # can read rather than lose it to somebody else's add.
        already_in = set(
            Participant.objects.filter(
                conversation=conversation, status=Participant.Status.ACTIVE
            ).values_list('pk', flat=True)
        )

        rows = [conversation.add_participant(user) for user in added]

        # Inside the same transaction, so a refused address undoes the whole
        # add rather than leaving half of it done - the rule above. The emails
        # themselves go out on commit, so a rollback sends none of them.
        adder = getattr(self.context.get('request'), 'user', None)
        for address in addresses:
            rows.append(membership.invite_by_email(conversation, address, adder))

        # Set for "all" too, not only "new": somebody coming back may carry a
        # cutoff from an earlier membership, and this add is the one that says
        # what they may read now. Before the announcement below, so the first
        # thing their app is sent already respects it.
        joining = [r.pk for r in rows if r is not None and r.pk not in already_in]
        if joining:
            cutoff = None
            if self.validated_data['history'] == 'new':
                cutoff = (
                    Message.objects.filter(conversation=conversation)
                    .order_by('-pk')
                    .values_list('pk', flat=True)
                    .first()
                )
            Participant.objects.filter(pk__in=joining).update(
                history_after_message_id=cutoff
            )

        if added:
            announce_added_to_conversation(conversation, added)

        return conversation


def _sees(request, conversation, message):
    """Whether the account behind this request may read this message.

    The history line drawn by "Only show new messages", for the write paths
    that would otherwise copy an old message's text back to its sender. True
    for anybody without a membership row here: those callers are refused for
    other reasons, by the checks around this one.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return True
    own = Participant.objects.filter(conversation=conversation, user=user).first()
    return own is None or own.sees_message(message.pk)


def validate_group_image(value):
    """The ceiling on a group's image, wherever it is written.

    ImageField has already proved the upload decodes as an image; this is only
    about not accepting a 40MB original - the same rule, and the same shape,
    as the avatar and attachment ceilings.

    It is a serializer check, so it does not cover the admin or the shell.
    That is already true of `Profile.avatar` and `Attachment.file`: the limit
    is there to bound a public endpoint and the storage bill, not to keep the
    data consistent, so a staff member uploading a large image deliberately is
    not something to defend against here.
    """
    if value and value.size > settings.MAX_GROUP_IMAGE_UPLOAD_BYTES:
        limit_mb = settings.MAX_GROUP_IMAGE_UPLOAD_BYTES // (1024 * 1024)
        raise serializers.ValidationError(f'Keep the image under {limit_mb}MB.')
    return value


def resolve_members(identifiers, exclude=None):
    """Turn usernames/phone numbers into users, or say which ones failed."""
    users = []
    unknown = []

    for identifier in identifiers:
        user = find_user(identifier, exclude=exclude)
        if user is None:
            unknown.append(identifier)
        elif user not in users:
            users.append(user)

    if unknown:
        raise serializers.ValidationError(
            f'No user found for: {", ".join(unknown)}.'
        )
    return users


# How many people the add-members search shows at once. Enough to pick from
# after a couple of letters; past that the answer is to keep typing, not to
# scroll a directory inside a dialog.
MEMBER_SEARCH_LIMIT = 5


def member_candidates(conversation, term, adder, context=None):
    """People `adder` could add to `conversation`, from what they typed.

    Returns (results, has_more, invite). Each result is the person as anybody may see
    them - name, handle, picture - with what adding them would do:

        ready           the add would go through
        already_member  in this Loop already, and re-adding is a no-op
        refused         the add would be refused, and `detail` says why

    Found the ways the rest of the product finds people, and no further:

        a username      by prefix, as `search_users` does
        an email        exact and verified only, as `find_user` does. A
                        prefix over addresses would let anybody walk the
                        directory a letter at a time, so a half-typed address
                        finds nobody.
        a number        exact and verified only, as `search_users` does

    Both obey Settings > Privacy and leave out either side of a block, so
    nobody can be found here who could not already be found by the search box,
    or added by typing their username, address or number.

    Every check the add endpoint makes is made here, in the same order, so a
    "ready" is not contradicted a moment later by the add.

    `invite` is set when a whole address found nobody: the address, offered for
    an email invitation, with the same status and detail a person gets.
    """
    from user.lookups import search_users

    term = (term or '').strip()
    invite = None
    if '@' in term:
        user = find_user(term, exclude=adder)
        if user is None and _is_own_address(adder, term):
            # Their own address. Already here - not somebody to invite.
            user = adder
        users, has_more = ([user] if user else []), False
        if user is None:
            invite = _email_invite_offer(conversation, term)
    else:
        found = list(search_users(term, exclude=adder)[:MEMBER_SEARCH_LIMIT + 1])
        users = found[:MEMBER_SEARCH_LIMIT]
        has_more = len(found) > MEMBER_SEARCH_LIMIT

    # One query for where every candidate stands in this Loop, rather than one
    # each - the page is small, but it is re-asked on every pause in typing.
    # From the table, not `conversation.participants`: the viewset prefetches
    # that as ACTIVE rows only, and a filter on it inherits the prefetch, so a
    # blocked row would never be seen.
    standing = dict(
        Participant.objects.filter(
            conversation=conversation, user__in=users
        ).values_list('user_id', 'status')
    )

    results = []
    for user in users:
        status, detail = _candidate_status(user, standing.get(user.pk), adder)
        results.append({
            'user': PublicUserSerializer(user, context=context or {}).data,
            'status': status,
            'detail': detail,
        })
    return results, has_more, invite


def _is_own_address(adder, term):
    address = (getattr(adder, 'email', '') or '').strip().lower()
    return bool(address) and address == term.strip().lower()


def _email_invite_offer(conversation, term):
    """An address nobody could be found at, as something to invite, or None.

    Offered for an address that belongs to a hidden account too, and that is
    deliberate: saying otherwise would say an account is there. Inviting one
    does nothing - `invite_by_email` adds nobody and emails nobody - which is
    the same answer the rest of the product gives.
    """
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.core.validators import validate_email

    address = term.strip().lower()
    try:
        validate_email(address)
    except DjangoValidationError:
        return None

    row = Participant.objects.filter(
        conversation=conversation, email__iexact=address, user__isnull=True
    ).first()
    status, detail = 'ready', ''
    if row is not None and row.status == Participant.Status.ACTIVE:
        status, detail = 'already_member', 'Already invited to this Loop.'
    elif row is not None and row.status == Participant.Status.BLOCKED:
        status, detail = 'refused', 'Blocked from this Loop.'
    return {'email': address, 'status': status, 'detail': detail}


def _candidate_status(user, standing, adder):
    """What adding `user` would do, as (status, detail).

    `standing` is their Participant status in this Loop, or None.
    """
    if standing == Participant.Status.ACTIVE:
        return 'already_member', 'Already in this Loop.'

    # `add_participant` refuses this row whatever anybody else allows. The
    # admin looking is the kind of person who put it there, so saying so
    # reveals nothing to them.
    if standing == Participant.Status.BLOCKED:
        return 'refused', 'Blocked from this Loop.'

    try:
        membership._refuse_blocked_invite(adder, user)
    except membership.JoinRefused as refusal:
        # The add endpoint's own generic wording, so nothing here tells a
        # block apart from any other refusal.
        return 'refused', str(refusal)

    if not privacy.allows_group_add(user, adder):
        return 'refused', privacy.group_add_refusal([user])

    return 'ready', ''


class JoinGroupSerializer(serializers.Serializer):
    """Join a group using the code from its shareable link.

    Always by code, never by id. A non-member cannot be handed a conversation
    by `get_queryset` anyway, and requiring the code stops the endpoint from
    becoming a way to walk group ids and join every open group in the product.
    Over SMS the distinction between OPEN and CODE reappears: OPEN takes a bare
    JOIN, CODE wants the code with it.
    """

    code = serializers.CharField(max_length=12)

    def save(self, **kwargs):
        from chat import membership

        code = self.validated_data['code'].strip()
        conversation = Conversation.objects.filter(
            kind=Conversation.Kind.GROUP, join_code__iexact=code
        ).first()

        # An unknown code and a code for a group that will not have you must be
        # answered identically, down to the envelope's message string. Raising
        # from validate_code instead would prefix one with the field name and
        # leave the other on `detail`, and that difference alone confirms which
        # codes are real - which is all an attacker harvesting codes needs.
        if conversation is None:
            raise membership.JoinRefused('That group code is not right.')

        try:
            return membership.request_to_join(
                conversation,
                user=self.context['request'].user,
                code=code,
            )
        except BlockedFromConversation:
            # Flattened to the same refusal a wrong code gets. A 403 here, where
            # every other bad code is a 400, tells the holder of a guessed code
            # that it is real - which is the whole of what an attacker
            # harvesting codes wants to learn.
            #
            # The SMS path deliberately keeps the two apart, because there it
            # decides between silence and a reply rather than between two
            # visible status codes.
            raise membership.JoinRefused('That group code is not right.')


class ParticipantActionSerializer(serializers.Serializer):
    """Name one membership row in this conversation to act on."""

    participant = serializers.IntegerField()

    def validate_participant(self, value):
        conversation = self.context['conversation']
        # Queried through Participant.objects, NOT conversation.participants.
        # The viewset's queryset prefetches participants filtered to ACTIVE, and
        # a related manager filters the prefetch cache rather than the table -
        # so going through the relation here would make every pending row
        # invisible to the one endpoint whose whole job is to act on them.
        row = Participant.objects.filter(
            conversation=conversation, pk=value
        ).first()
        if row is None:
            raise serializers.ValidationError('No such member of this conversation.')
        self.participant = row
        return value


class JoinRequestDecisionSerializer(ParticipantActionSerializer):
    decision = serializers.ChoiceField(choices=['approve', 'decline'])


class NudgeMemberSerializer(ParticipantActionSerializer):
    """Ask one member again about one channel.

    The channel is explicit rather than assumed to be SMS: the same member can
    be pending on email too, and which one an admin meant is not for the server
    to guess.
    """

    channel = serializers.ChoiceField(
        choices=ChannelConsent.Channel.choices,
        default=ChannelConsent.Channel.SMS,
    )


class RemoveMemberSerializer(ParticipantActionSerializer):
    block = serializers.BooleanField(
        required=False,
        default=False,
        help_text='Also stop them ever rejoining, by any route.',
    )


class ArchiveSerializer(serializers.Serializer):
    """Tidy a conversation away, or bring it back."""

    archived = serializers.BooleanField()


class MarkReadSerializer(serializers.Serializer):
    """How far the caller has read.

    `message_id` is optional: a client that just opened a conversation means
    "everything", and making it say which message that was would be a round
    trip to learn something the server already knows. Omitting it marks the
    newest message read.
    """

    message_id = serializers.IntegerField(required=False, min_value=1)


class DeliveryPreferenceSerializer(serializers.Serializer):
    """How one member wants one group delivered to them.

    Three separate switches, because they answer different questions:

      muted         silence this group entirely, on every channel. The member's
                    own act, and what texting MUTE sets.
      sms_enabled   whether SMS is one of this group's channels for them at all.
                    On by default; off means app-only.
      email_enabled the same question for email. Off is what the unsubscribe
                    link and a STOP EMAILS reply write, and it works both ways:
                    the group neither emails them nor takes their email
                    replies. This endpoint is the only thing that can turn it
                    back ON, because nothing reachable from an emailed token
                    may add.

    All optional, so a client changing one does not have to know the others.
    """

    muted = serializers.BooleanField(required=False)
    sms_enabled = serializers.BooleanField(required=False)
    email_enabled = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                'Set muted, sms_enabled, email_enabled, or any combination.'
            )
        return attrs


class ParticipantRoleSerializer(ParticipantActionSerializer):
    """Promote a member to admin, or demote them back.

    The join code is deliberately NOT on ConversationSerializer: it is the
    thing that lets a stranger in, so it is read from its own admin-only
    endpoint rather than handed to every member on every conversation fetch.
    """

    role = serializers.ChoiceField(choices=Participant.Role.choices)


class RenameMemberSerializer(ParticipantActionSerializer):
    """Give a member with no account a name the group can read.

    Somebody who joined by texting JOIN never gave one, so they show as a
    masked number. Only phone-only rows can be renamed: a member with an
    account has their own display name, and letting an admin overwrite it would
    let them relabel another person in the group.
    """

    display_name = serializers.CharField(max_length=80)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.participant.user_id is not None:
            raise serializers.ValidationError(
                'That member has an account; their own name is used.'
            )
        return attrs


class InviteByPhoneSerializer(serializers.Serializer):
    """Invite somebody to a group by their phone number.

    A name is required because this person has not chosen one - without it the
    group sees a masked number, and the admin adding them is the only person who
    knows who they are.
    """

    phone_number = serializers.CharField(max_length=16)
    display_name = serializers.CharField(max_length=80)

    def validate_phone_number(self, value):
        number = value.strip()
        phone_number_validator(number)
        return number
