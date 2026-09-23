"""Calls: the record of who was on one, and for how long.

Django is the source of truth for all of it - a call, who may join it, who did,
what plan allowed it. LiveKit is where the media actually flows, and knows
nothing about any of this beyond the room name and a token.

Nothing here holds audio or video. A recording is a row of metadata pointing at
an object in S3; the bytes never come near Postgres.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


def new_room_name():
    """A room name nobody can guess.

    Not the call's id: LiveKit rooms are addressed by name alone, so a name
    derived from a sequential id would let anybody with a token for one call
    guess the room of the next. A token is bound to a room, but there is no
    reason to make the name a hint at all.
    """
    return f'call-{uuid.uuid4().hex}'


class CallRoom(models.Model):
    """One call, in one conversation.

    Named for the LiveKit room it maps onto. `conversation` is what the product
    calls a group (or a one-to-one thread): a call belongs to the conversation
    it was started in, and only that conversation's active members can be in it.
    """

    class CallType(models.TextChoices):
        AUDIO = 'audio', 'Audio'
        VIDEO = 'video', 'Video'

    class Status(models.TextChoices):
        # Started, and nobody but the caller has picked up yet.
        RINGING = 'ringing', 'Ringing'
        # At least one other person has joined.
        ACTIVE = 'active', 'Active'
        ENDED = 'ended', 'Ended'

    class EndReason(models.TextChoices):
        # Somebody with the standing to end it for everyone did.
        ENDED = 'ended', 'Ended'
        # The last person left.
        EMPTY = 'empty', 'Everyone left'
        # A one-to-one call the other person turned down.
        DECLINED = 'declined', 'Declined'
        # A one-to-one call nobody answered.
        UNANSWERED = 'unanswered', 'Unanswered'
        # Hit CALL_MAX_DURATION_HOURS.
        TIMED_OUT = 'timed_out', 'Timed out'

    LIVE_STATUSES = (Status.RINGING, Status.ACTIVE)

    conversation = models.ForeignKey(
        'chat.Conversation',
        on_delete=models.CASCADE,
        related_name='calls',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='calls_started',
    )

    # Unique across every call ever made, because it is the only thing LiveKit
    # knows the call by.
    room_name = models.CharField(max_length=64, unique=True, default=new_room_name)

    call_type = models.CharField(
        max_length=5, choices=CallType.choices, default=CallType.AUDIO
    )
    status = models.CharField(
        max_length=8,
        choices=Status.choices,
        default=Status.RINGING,
        db_default=Status.RINGING,
    )
    end_reason = models.CharField(
        max_length=10, choices=EndReason.choices, blank=True, default='', db_default=''
    )
    # SET_NULL: deleting an account must not delete the record of a call other
    # people were on.
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    # When the call stopped ringing, i.e. the second person joined. Null for a
    # call that was never answered. `created_at` is when it started ringing.
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            # One live call per conversation, enforced by the database. Two
            # people pressing Call at the same moment both pass any check the
            # application makes first, and the loser of this is the only thing
            # that stops a conversation ringing twice.
            models.UniqueConstraint(
                fields=['conversation'],
                condition=Q(status__in=['ringing', 'active']),
                name='calls_one_live_call_per_conversation',
            ),
        ]
        indexes = [
            models.Index(fields=['conversation', '-created_at']),
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f'{self.get_call_type_display()} call {self.pk} ({self.status})'

    @property
    def is_live(self):
        return self.status in self.LIVE_STATUSES


class CallParticipant(models.Model):
    """One person's time in one call.

    One row per stay rather than per person: somebody who drops and comes back
    has two, so the history says what happened. At most one is OPEN (left_at
    null) per person per call - that is what makes joining twice idempotent.

    A row is written when the token is ISSUED, not when the person connects,
    because Django cannot see the connection. `connected_at` is filled in when
    LiveKit says they arrived; a row that never gets one is somebody who asked
    and did not come, and `token_expires_at` is how the sweep knows to give
    their place back.
    """

    call_room = models.ForeignKey(
        CallRoom, on_delete=models.CASCADE, related_name='participants'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='call_participations',
    )

    joined_at = models.DateTimeField(auto_now_add=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)
    # Of the stay, joined_at to left_at. Stored, so a call history is one read.
    duration = models.DurationField(null=True, blank=True)

    token_expires_at = models.DateTimeField()

    class Meta:
        ordering = ['joined_at', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['call_room', 'user'],
                condition=Q(left_at__isnull=True),
                name='calls_one_open_stay_per_user',
            ),
        ]
        indexes = [models.Index(fields=['call_room', 'left_at'])]

    def __str__(self):
        return f'{self.user_id} in call {self.call_room_id}'


class CallEvent(models.Model):
    """One thing that happened in a call, in order.

    The participant rows say who was there; this says what was done - including
    the things that leave no participant row, like turning a call down.
    """

    class Type(models.TextChoices):
        STARTED = 'started', 'Call started'
        JOINED = 'joined', 'Joined'
        LEFT = 'left', 'Left'
        REJECTED = 'rejected', 'Turned down'
        ENDED = 'ended', 'Call ended'
        RECORDING_STARTED = 'recording_started', 'Recording started'
        RECORDING_STOPPED = 'recording_stopped', 'Recording stopped'

    call_room = models.ForeignKey(
        CallRoom, on_delete=models.CASCADE, related_name='events'
    )
    # Null for what the system did on its own - a timeout, LiveKit reporting a
    # room closed.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'{self.type} @ call {self.call_room_id}'


class CallRecording(models.Model):
    """The metadata of one recording. The file itself lives in S3.

    Owned by whoever started it (`started_by`): they are the person the other
    participants were told was recording. Everything a client needs to decide
    what to show is here; the object key is never sent to one.
    """

    class Status(models.TextChoices):
        # Asked LiveKit to start, no answer yet.
        STARTING = 'starting', 'Starting'
        RECORDING = 'recording', 'Recording'
        # Stop asked for; the file is still being finalised and uploaded.
        STOPPING = 'stopping', 'Stopping'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        # The file is gone - deleted by a person or by retention. The row stays,
        # so a call still says it was recorded.
        DELETED = 'deleted', 'Deleted'

    IN_FLIGHT = (Status.STARTING, Status.RECORDING, Status.STOPPING)

    call_room = models.ForeignKey(
        CallRoom, on_delete=models.CASCADE, related_name='recordings'
    )
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='call_recordings_started',
    )
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.STARTING,
        db_default=Status.STARTING,
    )
    # LiveKit's id for the recording job. Empty until it answers.
    egress_id = models.CharField(max_length=128, blank=True, default='', db_default='')
    error = models.CharField(max_length=500, blank=True, default='', db_default='')

    # Where the file is. Blanked when it is deleted, so nothing keeps a pointer
    # to an object that is not there.
    s3_bucket = models.CharField(max_length=255, blank=True, default='', db_default='')
    s3_key = models.CharField(max_length=512, blank=True, default='', db_default='')
    content_type = models.CharField(max_length=40, blank=True, default='', db_default='')
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    # When retention deletes it. Null keeps it until somebody deletes it.
    expires_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at', '-id']
        constraints = [
            # One recording in progress per call, by the database - a double
            # tap on Record must not start two egress jobs and bill for both.
            models.UniqueConstraint(
                fields=['call_room'],
                condition=Q(status__in=['starting', 'recording', 'stopping']),
                name='calls_one_recording_in_flight',
            ),
        ]
        indexes = [
            models.Index(fields=['egress_id']),
            models.Index(fields=['status', 'expires_at']),
        ]

    def __str__(self):
        return f'Recording {self.pk} of call {self.call_room_id} ({self.status})'

    @property
    def is_in_flight(self):
        return self.status in self.IN_FLIGHT
