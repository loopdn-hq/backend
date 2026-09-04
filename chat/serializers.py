"""Serializers for conversations, membership and messages."""
import logging

from django.conf import settings
from django.db import transaction
from rest_framework import serializers

from chat.models import (
    Attachment,
    BlockedFromConversation,
    Conversation,
    Message,
    Participant,
)
from chat.services import announce_added_to_conversation, create_message
from user.lookups import find_user
from user.models import phone_number_validator
from user.serializers import PublicUserSerializer

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


class MessageSerializer(serializers.ModelSerializer):
    sender = serializers.SerializerMethodField()
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Message
        fields = [
            'id',
            'conversation',
            'sender',
            'text',
            'source',
            'attachments',
            'created_at',
        ]
        read_only_fields = fields

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
    # Null for a member who has no account. `display_label` is the field a
    # client should render: it works for both kinds and needs no branching.
    user = PublicUserSerializer(read_only=True)
    display_label = serializers.CharField(read_only=True)
    is_phone_only = serializers.BooleanField(read_only=True)

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
            'joined_at',
        ]
        read_only_fields = fields


class GroupNumberSerializer(serializers.Serializer):
    """The group's own phone number, as a client needs to see it.

    A bare string was enough while the only question was "what do I dial". It
    cannot answer "can I dial it yet": with no provider configured, ordering
    mints a well-formed placeholder that belongs to nobody, and a client that
    renders it tells the user to text a dead line. An empty `provider` is
    exactly that case, so it is the one field here that is not decoration.

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
    # The number members text to reach this group. Read-only everywhere and
    # for everybody: it is fixed for the life of the group, so there is no
    # writable form of this field for a client or an admin to reach.
    sms_number = serializers.SerializerMethodField()

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
            'sms_number',
            'last_message',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

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
            # A direct conversation cannot contain a phone-only member today,
            # but titling one after a member with no account would crash on
            # `.user.profile` rather than degrade, so it is skipped explicitly.
            if participant.user_id is None:
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
        if message is None:
            return None
        return MessageSerializer(message, context=self.context).data

    def _viewer(self):
        request = self.context.get('request')
        return getattr(request, 'user', None) if request else None


class ConversationSearchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Conversation
        fields = ['id', 'name', 'join_mode', 'mode']
        read_only_fields = fields


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
            announce_added_to_conversation(conversation, [self.other])
        return conversation


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
}


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
        help_text='Usernames or verified +E.164 numbers of EXISTING accounts.',
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
    settings = GroupSettingsFieldsSerializer(required=False)

    def validate_image(self, value):
        return validate_group_image(value)

    def validate_members(self, values):
        return resolve_members(values, exclude=self.context['request'].user)

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

        return conversation

    def _create_group(self, validated_data, creator):
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

        return conversation

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
        return instance


class AddMembersSerializer(serializers.Serializer):
    """Add people to a group by username or phone number."""

    members = serializers.ListField(child=serializers.CharField(), allow_empty=False)

    def validate_members(self, values):
        return resolve_members(values, exclude=None)

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

        # add_participant is idempotent, so re-adding an existing member is
        # a no-op rather than an error - but they should not be told they
        # were just added, either.
        added = [
            user for user in users if not conversation.has_participant(user)
        ]
        for user in added:
            conversation.add_participant(user)

        if added:
            announce_added_to_conversation(conversation, added)

        return conversation


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


class RemoveMemberSerializer(ParticipantActionSerializer):
    block = serializers.BooleanField(
        required=False,
        default=False,
        help_text='Also stop them ever rejoining, by any route.',
    )


class DeliveryPreferenceSerializer(serializers.Serializer):
    """How one member wants one group delivered to them.

    Two separate switches, because they answer different questions:

      muted        silence this group entirely, on every channel. The member's
                   own act, and what texting MUTE sets.
      sms_enabled  whether SMS is one of this group's channels for them at all.
                   On by default; off means app-only.

    Both optional, so a client changing one does not have to know the other.
    """

    muted = serializers.BooleanField(required=False)
    sms_enabled = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                'Set muted, sms_enabled, or both.'
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
