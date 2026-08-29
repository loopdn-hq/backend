"""Serializers for conversations, membership and messages."""
from django.conf import settings
from rest_framework import serializers

from chat.models import Attachment, Conversation, Message, Participant
from chat.services import broadcast_added_to_conversation, create_message
from user.lookups import find_user
from user.serializers import PublicUserSerializer


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


class MessageSerializer(serializers.ModelSerializer):
    sender = PublicUserSerializer(read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Message
        fields = ['id', 'conversation', 'sender', 'text', 'attachments', 'created_at']
        read_only_fields = fields


class MessageCreateSerializer(serializers.Serializer):
    """Text, attachments, or both - but not neither.

    Emoji and links need nothing special: they are characters in `text`.
    """

    text = serializers.CharField(required=False, allow_blank=True)
    attachments = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
    )

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
        if not (attrs.get('text') or '').strip() and not attrs.get('attachments'):
            raise serializers.ValidationError(
                'Send some text or at least one attachment.'
            )
        return attrs

    def create(self, validated_data):
        return create_message(
            conversation=validated_data['conversation'],
            sender=validated_data['sender'],
            text=validated_data.get('text', ''),
            uploads=validated_data.get('attachments'),
        )


class ParticipantSerializer(serializers.ModelSerializer):
    user = PublicUserSerializer(read_only=True)

    class Meta:
        model = Participant
        fields = ['user', 'role', 'joined_at']
        read_only_fields = fields


class ConversationSerializer(serializers.ModelSerializer):
    participants = ParticipantSerializer(many=True, read_only=True)
    title = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id',
            'kind',
            'name',
            'title',
            'participants',
            'last_message',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_title(self, obj):
        """What to show in a conversation list.

        A group has a name. A direct conversation is named after whoever the
        viewer is talking to, so it depends on who is asking.
        """
        if obj.kind == Conversation.Kind.GROUP:
            return obj.name

        viewer = self._viewer()
        for participant in obj.participants.all():
            if viewer is None or participant.user_id != viewer.pk:
                return participant.user.profile.display_label
        return ''

    def get_last_message(self, obj):
        # Populated by the list view's Prefetch; falls back to a query for
        # the detail view, where one extra query costs nothing.
        prefetched = getattr(obj, 'latest_messages', None)
        message = prefetched[0] if prefetched else None
        if prefetched is None:
            message = obj.messages.first()
        if message is None:
            return None
        return MessageSerializer(message, context=self.context).data

    def _viewer(self):
        request = self.context.get('request')
        return getattr(request, 'user', None) if request else None


class DirectConversationSerializer(serializers.Serializer):
    """Start (or find) a one-to-one conversation with somebody."""

    identifier = serializers.CharField(
        help_text='A username, or a phone number in +E.164 form.'
    )

    def validate_identifier(self, value):
        viewer = self.context['request'].user
        other = find_user(value, exclude=viewer)
        if other is None:
            # Deliberately vague: confirming "no such user" for arbitrary
            # input turns this into a directory oracle.
            raise serializers.ValidationError('No user found with that username or number.')
        self.other = other
        return value

    def create(self, validated_data):
        conversation, created = Conversation.start_direct(
            self.context['request'].user, self.other
        )
        if created:
            broadcast_added_to_conversation(conversation, [self.other])
        return conversation


class GroupCreateSerializer(serializers.Serializer):
    """Create a group. Whoever calls this becomes its admin."""

    name = serializers.CharField(max_length=120)
    members = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text='Usernames or +E.164 phone numbers.',
    )

    def validate_members(self, values):
        return resolve_members(values, exclude=self.context['request'].user)

    def create(self, validated_data):
        creator = self.context['request'].user
        conversation = Conversation.start_group(creator, validated_data['name'])

        members = validated_data.get('members') or []
        for user in members:
            conversation.add_participant(user)
        if members:
            broadcast_added_to_conversation(conversation, members)

        return conversation


class AddMembersSerializer(serializers.Serializer):
    """Add people to a group by username or phone number."""

    members = serializers.ListField(child=serializers.CharField(), allow_empty=False)

    def validate_members(self, values):
        return resolve_members(values, exclude=None)

    def save(self, **kwargs):
        conversation = self.context['conversation']
        users = self.validated_data['members']

        # add_participant is idempotent, so re-adding an existing member is
        # a no-op rather than an error - but they should not be told they
        # were just added, either.
        added = [
            user for user in users if not conversation.has_participant(user)
        ]
        for user in added:
            conversation.add_participant(user)

        if added:
            broadcast_added_to_conversation(conversation, added)

        return conversation


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
