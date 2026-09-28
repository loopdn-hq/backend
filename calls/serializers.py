"""How calls and recordings are shown, and what a request to start one may say."""
from rest_framework import serializers

from calls.models import CallParticipant, CallRecording, CallRoom
from user.serializers import PublicUserSerializer


class CallParticipantSerializer(serializers.ModelSerializer):
    user = PublicUserSerializer(read_only=True)
    # Seconds, not a timedelta: DRF would render "00:03:12.5", which every
    # client would have to parse for a number it can use directly.
    duration = serializers.SerializerMethodField()

    class Meta:
        model = CallParticipant
        fields = ['id', 'user', 'joined_at', 'left_at', 'duration']
        read_only_fields = fields

    def get_duration(self, obj):
        return int(obj.duration.total_seconds()) if obj.duration is not None else None


class CallSerializer(serializers.ModelSerializer):
    """A call, as REST returns it and as the socket broadcasts it.

    One shape for both, so a client reads a `call.incoming` frame with the same
    code as a `GET /calls/<id>/`.

    `participants` is every stay, not only who is in right now, because a call's
    history is part of the record; a client shows the ones whose `left_at` is
    null. There is no `token` here or anywhere a broadcast can reach: that is
    handed only to the person it was minted for.
    """

    conversation = serializers.PrimaryKeyRelatedField(read_only=True)
    created_by = PublicUserSerializer(read_only=True)
    participants = CallParticipantSerializer(many=True, read_only=True)
    participant_count = serializers.SerializerMethodField()
    is_recording = serializers.SerializerMethodField()

    class Meta:
        model = CallRoom
        fields = [
            'id',
            'conversation',
            'call_type',
            'status',
            'end_reason',
            'room_name',
            'created_by',
            'started_at',
            'ended_at',
            'created_at',
            'participant_count',
            'is_recording',
            'participants',
        ]
        read_only_fields = fields

    def get_participant_count(self, obj):
        # Over the prefetched stays, so it costs no query of its own.
        return sum(1 for stay in obj.participants.all() if stay.left_at is None)

    def get_is_recording(self, obj):
        annotated = getattr(obj, 'recording_active', None)
        if annotated is not None:
            return annotated
        return obj.recordings.filter(status=CallRecording.Status.RECORDING).exists()


class CallStartSerializer(serializers.Serializer):
    conversation = serializers.IntegerField(min_value=1)
    call_type = serializers.ChoiceField(
        choices=CallRoom.CallType.choices, default=CallRoom.CallType.AUDIO
    )


class RecordingSerializer(serializers.ModelSerializer):
    """A recording's metadata.

    Never the bucket or the key: they say where the file is, and the only thing
    that should ever be handed out is a link signed for one person, one moment.
    """

    call = serializers.PrimaryKeyRelatedField(source='call_room', read_only=True)
    started_by = PublicUserSerializer(read_only=True)

    class Meta:
        model = CallRecording
        fields = [
            'id',
            'call',
            'status',
            'started_by',
            'started_at',
            'ended_at',
            'duration_seconds',
            'size_bytes',
            'content_type',
            'expires_at',
            'deleted_at',
        ]
        read_only_fields = fields
