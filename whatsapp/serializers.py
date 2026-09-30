"""What the web app sends and sees for WhatsApp.

The token is in none of these, in either direction. It goes from Meta to the
database and from the database to Meta, and nowhere else.
"""
from urllib.parse import quote

from rest_framework import serializers

from user.models import phone_number_validator
from whatsapp.models import GroupChannel, WhatsAppConnection


class ConnectSerializer(serializers.Serializer):
    """The three things Embedded Signup gives the browser."""

    code = serializers.CharField(max_length=2048)
    waba_id = serializers.RegexField(r'^\d{1,64}$')
    phone_number_id = serializers.RegexField(r'^\d{1,64}$')
    business_id = serializers.RegexField(r'^\d{1,64}$', required=False, allow_blank=True)


class ConnectionSerializer(serializers.ModelSerializer):
    groups = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppConnection
        fields = [
            'id', 'status', 'waba_id', 'phone_number_id', 'display_phone_number',
            'verified_name', 'quality_rating', 'messaging_limit_tier',
            'notification_template_name', 'notification_template_language',
            'invitation_template_name', 'last_error', 'connected_at', 'groups',
        ]
        read_only_fields = [
            f for f in fields
            if f not in ('notification_template_name', 'notification_template_language',
                         'invitation_template_name')
        ]

    def get_groups(self, connection):
        return [
            {'id': c.conversation_id, 'name': c.conversation.name, 'keyword': c.keyword}
            for c in connection.group_channels.select_related('conversation')
        ]


class AttachSerializer(serializers.Serializer):
    connection = serializers.IntegerField()
    keyword = serializers.CharField(max_length=16, required=False, allow_blank=True)
    delivery_enabled = serializers.BooleanField(required=False)
    replies_enabled = serializers.BooleanField(required=False)


class GroupChannelSerializer(serializers.ModelSerializer):
    """How WhatsApp looks from inside one group, to one member."""

    display_phone_number = serializers.CharField(
        source='whatsapp_connection.display_phone_number', read_only=True
    )
    verified_name = serializers.CharField(
        source='whatsapp_connection.verified_name', read_only=True
    )
    connection_status = serializers.CharField(
        source='whatsapp_connection.status', read_only=True
    )
    # Opens a chat with the business number, pre-filled with START - the
    # one-tap opt-in for a member who is looking at the group in the app.
    join_link = serializers.SerializerMethodField()

    class Meta:
        model = GroupChannel
        fields = [
            'id', 'keyword', 'display_phone_number', 'verified_name',
            'connection_status', 'delivery_enabled', 'replies_enabled', 'join_link',
        ]

    def get_join_link(self, channel):
        digits = ''.join(ch for ch in channel.whatsapp_connection.display_phone_number
                         if ch.isdigit())
        return f'https://wa.me/{digits}?text={quote("START")}' if digits else ''


class ConsentSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class InviteSerializer(serializers.Serializer):
    """Either a new number, or somebody already in the group."""

    phone_number = serializers.CharField(
        max_length=16, required=False, validators=[phone_number_validator]
    )
    display_name = serializers.CharField(max_length=80, required=False, allow_blank=True)
    participant = serializers.IntegerField(required=False)
    connection = serializers.IntegerField(required=False)

    def validate(self, attrs):
        if bool(attrs.get('phone_number')) == bool(attrs.get('participant')):
            raise serializers.ValidationError(
                'Give either a phone number or a member, not both.'
            )
        if attrs.get('phone_number') and not (attrs.get('display_name') or '').strip():
            # A phone-only member with no name shows as a bare number to the
            # whole group - see Participant's phone_participant_has_a_name.
            raise serializers.ValidationError({'display_name': 'A name is required.'})
        return attrs
