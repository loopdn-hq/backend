"""Everything that changes the state of a call.

The REST endpoints, LiveKit's webhook and the Celery sweep all come through
here, so a call is started, joined, left, ended and recorded exactly one way no
matter what caused it - the reason `chat/services.py` and `chat/membership.py`
are shaped the same.

The rules, in one place:

    who may be in a call     the ACTIVE members of the conversation it was
                             started in, and nobody else
    whether it may start     the plan's `has_calls` / `has_video_calls` -
                             Free through Business ship with both on; a custom
                             Enterprise plan only has them if the contract
                             bought them (enterprise.services.FEATURE_KEYS)
    how many                 the plan's `max_call_participants`, counted
                             under a row lock so two people cannot both take
                             the last place
    what they may publish    decided here from the call type and the plan and
                             written into the token, so a client cannot ask for
                             more
    who may end / record     whoever started the call, or a group admin

State changes take a lock on the call row and run in one transaction. Anything
that talks to LiveKit or S3 runs AFTER the commit (or, for starting a recording,
between two short transactions) - a provider call inside an open transaction
holds a database connection and a row lock across a network round trip.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.utils import timezone
from livekit import api as lk
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from billing.limits import plan_for
from calls import livekit, storage
from calls.livekit import CallServiceError, LiveKitNotFound
from calls.models import CallEvent, CallParticipant, CallRecording, CallRoom
from chat.models import Conversation, Participant
from chat.services import conversation_group, send_to_group
from utils.dispatch import after_commit
from calls.tasks import close_room_task

logger = logging.getLogger(__name__)

# A token that has expired stops mattering here a little later than it does to
# LiveKit, so somebody connecting in the last second is not dropped from under
# themselves by a sweep that ran at the wrong moment.
STALE_STAY_GRACE = timedelta(seconds=60)

# A recording still "in progress" this long after its call ended has had its
# webhook lost, and is asked about directly.
RECORDING_RECONCILE_AFTER = timedelta(minutes=5)


class Conflict(APIException):
    """The request is fine but the call is not in a state that allows it."""

    status_code = 409
    default_detail = 'That is not possible in the call\'s current state.'
    default_code = 'conflict'


# -- reading -------------------------------------------------------------------


def call_queryset():
    """Calls as the API renders them: creator, stays and recording state joined.

    One definition, so the REST response, the socket frame and the list all read
    the same rows the same way and none of them costs a query per participant.
    """
    return (
        CallRoom.objects.select_related('created_by__profile')
        .prefetch_related(
            Prefetch(
                'participants',
                queryset=CallParticipant.objects.select_related('user__profile'),
            )
        )
        .annotate(
            recording_active=Exists(
                CallRecording.objects.filter(
                    call_room=OuterRef('pk'), status=CallRecording.Status.RECORDING
                )
            )
        )
    )


def calls_visible_to(user):
    """The calls this person may see: those of conversations they are in.

    The access rule, as a queryset - the way `ConversationViewSet.get_queryset`
    does it - so a call in somebody else's conversation is a 404 and not a 403,
    and nobody can learn that it exists.
    """
    return call_queryset().filter(
        conversation__participants__user=user,
        conversation__participants__status=Participant.Status.ACTIVE,
    ).distinct()


def _membership(user, conversation):
    return Participant.objects.filter(
        conversation=conversation, user=user, status=Participant.Status.ACTIVE
    ).first()


def _require_member(user, conversation):
    member = _membership(user, conversation)
    if member is None:
        raise PermissionDenied('You are not a member of this conversation.')
    return member


def plan_for_call(call_or_conversation, initiator=None):
    """The plan whose limits apply to a call.

    The GROUP OWNER's, exactly as members-per-group works
    (`billing.limits.check_can_add_members`): a plan controls what its owner's
    groups may do, whichever member happens to be using them today. A direct
    conversation has no owner in that sense, so it is the person who started the
    call.
    """
    if isinstance(call_or_conversation, CallRoom):
        conversation, initiator = call_or_conversation.conversation, call_or_conversation.created_by
    else:
        conversation = call_or_conversation

    if conversation.kind == Conversation.Kind.GROUP:
        return plan_for(conversation.created_by)
    return plan_for(initiator)


def may_administer(user, call):
    """Who may end a call for everybody, and start or stop its recording.

    Whoever started it, or an admin of the group. Direct conversations have no
    admins - neither person outranks the other - so either may.
    """
    if user.pk == call.created_by_id:
        return True
    conversation = call.conversation
    if conversation.kind == Conversation.Kind.DIRECT:
        return conversation.has_participant(user)
    return conversation.is_admin(user)


def may_view_recordings(user, call):
    """Who may see and download a call's recordings.

    An active member of the conversation who ran the call, administers the
    group, or was actually on it. Not every member: a recording holds every
    participant's voice, and somebody who was never there has no claim to it.
    """
    if _membership(user, call.conversation) is None:
        return False
    if may_administer(user, call):
        return True
    return call.participants.filter(user=user).exists()


def _sources_for(call, plan):
    sources = [livekit.MICROPHONE]
    if call.call_type == CallRoom.CallType.VIDEO:
        sources.append(livekit.CAMERA)
    if plan.has_screen_share:
        sources += [livekit.SCREEN_SHARE, livekit.SCREEN_SHARE_AUDIO]
    return sources


# -- broadcasting --------------------------------------------------------------
#
# Over the socket that already exists: every frame goes to the conversation's
# group, which everybody who may see the call is already in. Nothing here opens
# a channel of its own. Each type needs a matching method on ChatConsumer
# (`call.incoming` -> `call_incoming`), or Channels drops the frame in silence.

INCOMING = 'call.incoming'
ACCEPTED = 'call.accepted'
REJECTED = 'call.rejected'
ENDED = 'call.ended'
RECORDING_STARTED = 'call.recording.started'
RECORDING_STOPPED = 'call.recording.stopped'


def _broadcast(call, event_type, **extra):
    """Tell the conversation, once the transaction that caused it has committed.

    The payload is built after the commit from a fresh read, for the reason
    `chat.services.subscribe_to_conversation` gives: a payload built from the
    caller's copy would describe the call as it was before the change.
    """
    call_id, conversation_id = call.pk, call.conversation_id

    def _send():
        from calls.serializers import CallSerializer

        try:
            fresh = call_queryset().filter(pk=call_id).first()
            if fresh is None:
                return
            send_to_group(
                conversation_group(conversation_id),
                {
                    'type': event_type,
                    'call_id': call_id,
                    'conversation_id': conversation_id,
                    'call': CallSerializer(fresh).data,
                    **extra,
                },
            )
        except Exception:
            # This runs after the commit, so raising would turn a call that
            # really happened into a 500 for the person who made it.
            logger.exception('Could not broadcast %s for call %s', event_type, call_id)

    transaction.on_commit(_send)


def _record(call, event_type, user=None):
    return CallEvent.objects.create(call_room=call, user=user, type=event_type)


# -- starting and joining ------------------------------------------------------


def _refuse_if_blocked(user, conversation):
    """A direct call between people who have blocked each other is refused.

    Same rule and same copy as `chat.services._refuse_if_blocked`, for the same
    reason: the blocker is told plainly, the blocked person is told something no
    different from any other refusal, so a block cannot be discovered by
    calling.
    """
    if conversation.kind != Conversation.Kind.DIRECT:
        return

    other = (
        Participant.objects.filter(conversation=conversation, user__isnull=False)
        .exclude(user=user)
        .select_related('user')
        .first()
    )
    if other is None:
        return

    from user.blocks import blocks_between, has_blocked

    if not blocks_between(user, other.user):
        return
    if has_blocked(user, other.user):
        raise PermissionDenied('You blocked this person. Unblock them to call.')
    raise PermissionDenied('Unable to start the call.')


def _issue_token(call, user, plan, stay):
    """Mint a token for this stay, and record when it stops being usable."""
    stay.token_expires_at = timezone.now() + timedelta(
        seconds=settings.LIVEKIT_TOKEN_TTL_SECONDS
    )
    stay.save(update_fields=['token_expires_at'])

    profile = getattr(user, 'profile', None)
    token = livekit.mint_token(
        room_name=call.room_name,
        identity=user.pk,
        name=profile.display_label if profile else user.get_username(),
        sources=_sources_for(call, plan),
    )
    return {
        'url': settings.LIVEKIT_URL,
        'token': token,
        'expires_at': stay.token_expires_at,
    }


def _open_stay(call, user, plan):
    """Give this person a place in the call, or take their existing one.

    Returns `(stay, created)`. Called with the call row locked, which is what
    makes the count below true: nobody else can add a stay between the count
    and the insert.
    """
    stay = call.participants.filter(user=user, left_at__isnull=True).first()
    if stay is not None:
        return stay, False

    cap = plan.max_call_participants
    if cap is not None:
        in_call = call.participants.filter(left_at__isnull=True).count()
        if in_call >= cap:
            raise PermissionDenied(
                f'This call is full. The {plan.name} plan allows up to {cap} '
                f'{"person" if cap == 1 else "people"} on a call.'
            )

    stay = CallParticipant.objects.create(
        call_room=call,
        user=user,
        token_expires_at=timezone.now(),  # replaced by _issue_token, straight after
    )
    _record(call, CallEvent.Type.JOINED, user)
    return stay, True


def start_call(user, conversation, call_type):
    """Start a call in a conversation and put the caller in it.

    Returns `(call, livekit)`, where `livekit` is what the caller's client needs
    to connect: the server address and a token for this call's room.
    """
    livekit.require_configured()
    member = _require_member(user, conversation)

    # Whoever could not post here does not start a call here either: in a
    # broadcast group, members receive.
    if not conversation.allows_posting_by(member):
        raise PermissionDenied('Only admins can start a call in this group.')
    _refuse_if_blocked(user, conversation)

    plan = plan_for_call(conversation, initiator=user)
    if not plan.has_calls:
        raise PermissionDenied(
            f'The {plan.name} plan does not include calling. Upgrade the '
            'group owner\'s plan to make calls.'
        )
    if call_type == CallRoom.CallType.VIDEO and not plan.has_video_calls:
        raise PermissionDenied(
            f'The {plan.name} plan does not include video calls. Upgrade the '
            'group owner\'s plan, or start an audio call instead.'
        )

    with transaction.atomic():
        try:
            # A savepoint: the unique constraint is the real guard against two
            # calls in one conversation, and its IntegrityError must not poison
            # the transaction the way it would without one.
            with transaction.atomic():
                call = CallRoom.objects.create(
                    conversation=conversation, created_by=user, call_type=call_type
                )
        except IntegrityError:
            live = CallRoom.objects.filter(
                conversation=conversation, status__in=CallRoom.LIVE_STATUSES
            ).first()
            conflict = Conflict('There is already a call in progress here.')
            # After construction, because DRF turns every value in an error
            # into a string - and a client wants a number it can join by.
            conflict.detail = {
                'detail': conflict.detail,
                'call_id': live.pk if live else None,
            }
            raise conflict

        _record(call, CallEvent.Type.STARTED, user)
        stay, _ = _open_stay(call, user, plan)
        connection = _issue_token(call, user, plan, stay)
        _broadcast(call, INCOMING, user_id=user.pk)

    return call, connection


def join_call(user, call_id):
    """Let a member into a live call, or hand a member already in a fresh token.

    Joining twice is not an error and does not take a second place: it is what a
    page refresh does, and it needs a new token because the old one is used up
    the moment LiveKit accepts it.
    """
    livekit.require_configured()

    with transaction.atomic():
        call = _lock(call_id)
        if not call.is_live:
            raise Conflict('This call has ended.')

        _require_member(user, call.conversation)
        plan = plan_for_call(call)

        stay, created = _open_stay(call, user, plan)
        connection = _issue_token(call, user, plan, stay)

        if created and user.pk != call.created_by_id:
            if call.status == CallRoom.Status.RINGING:
                call.status = CallRoom.Status.ACTIVE
                call.started_at = timezone.now()
                call.save(update_fields=['status', 'started_at'])
            _broadcast(call, ACCEPTED, user_id=user.pk)

    return call, connection


# -- leaving, declining, ending ------------------------------------------------


def _lock(call_id):
    """The call row, locked until the transaction ends.

    `of=('self',)` so the lock is on the call alone. Without it Postgres would
    lock the joined conversation row as well, and every message posted in that
    conversation - which touches it - would queue behind a call operation.
    """
    return (
        CallRoom.objects.select_for_update(of=('self',))
        .select_related('conversation')
        .get(pk=call_id)
    )


def _close_stay(stay, now):
    stay.left_at = now
    stay.duration = now - stay.joined_at
    stay.save(update_fields=['left_at', 'duration'])


def _end(call, reason, by=None):
    """End a call. The caller holds the lock and has checked it is still live."""
    now = timezone.now()
    for stay in call.participants.filter(left_at__isnull=True):
        _close_stay(stay, now)

    call.status = CallRoom.Status.ENDED
    call.end_reason = reason
    call.ended_by = by
    call.ended_at = now
    call.save(update_fields=['status', 'end_reason', 'ended_by', 'ended_at'])
    _record(call, CallEvent.Type.ENDED, by)

    _broadcast(call, ENDED, user_id=by.pk if by else None)

    # Disconnects anybody still holding a connection, which is what makes
    # "ended" true on LiveKit's side and not just in our table. A recording in
    # the room is finished by LiveKit itself when the room closes.
    room_name = call.room_name
    after_commit(
        f'close room {room_name}',
        lambda: close_room_task.delay(room_name),
        lambda: close_room(room_name),
    )


def close_room(room_name):
    """Close a room on LiveKit. Logged and swallowed: the call is already over."""
    try:
        livekit.delete_room(room_name)
    except CallServiceError as exc:
        # No traceback: livekit._run has already logged why, once. This only
        # says which room was left open.
        logger.warning('Could not close LiveKit room %s: %s', room_name, exc)


def leave_call(user, call_id):
    """Take this person out of a call. The last one out ends it.

    Leaving a call you are not in is not an error: a client that navigated away
    and retries has nothing to be told off for, and the answer it wants ("you
    are not in this call") is already true.
    """
    with transaction.atomic():
        call = _lock(call_id)
        _leave(call, user)
    return call


def _leave(call, user):
    """The body of leaving, for a caller that already holds the lock."""
    stay = call.participants.filter(user=user, left_at__isnull=True).first()
    if stay is None:
        return

    _close_stay(stay, timezone.now())
    _record(call, CallEvent.Type.LEFT, user)

    if call.is_live and not call.participants.filter(left_at__isnull=True).exists():
        # Hanging up before anybody answered is a call nobody picked up.
        _end(
            call,
            CallRoom.EndReason.UNANSWERED
            if call.status == CallRoom.Status.RINGING
            else CallRoom.EndReason.EMPTY,
            by=user,
        )


def reject_call(user, call_id):
    """Turn down a call that is ringing.

    In a group this only says "not me" - the call carries on for everybody else.
    A one-to-one call has nobody else, so turning it down ends it.
    """
    with transaction.atomic():
        call = _lock(call_id)
        _require_member(user, call.conversation)

        if user.pk == call.created_by_id:
            raise ValidationError('You cannot turn down your own call. Leave it instead.')
        if not call.is_live:
            return call  # too late to matter, and nothing to be told off for
        if call.participants.filter(user=user, left_at__isnull=True).exists():
            raise ValidationError('You are already in this call.')

        already = call.events.filter(type=CallEvent.Type.REJECTED, user=user).exists()
        if not already:
            _record(call, CallEvent.Type.REJECTED, user)
            _broadcast(call, REJECTED, user_id=user.pk)

        if (
            call.conversation.kind == Conversation.Kind.DIRECT
            and call.status == CallRoom.Status.RINGING
        ):
            _end(call, CallRoom.EndReason.DECLINED, by=user)
    return call


def end_call(user, call_id):
    """End a call for everybody. Whoever started it, or a group admin."""
    with transaction.atomic():
        call = _lock(call_id)
        _require_member(user, call.conversation)

        if not may_administer(user, call):
            raise PermissionDenied(
                'Only the person who started the call or a group admin can end it.'
            )
        if call.is_live:
            _end(call, CallRoom.EndReason.ENDED, by=user)
    return call


# -- recording -----------------------------------------------------------------


def _recording_key(call, audio_only):
    import uuid

    extension = 'ogg' if audio_only else 'mp4'
    # A random name, so a key cannot be guessed from a call id - the bucket is
    # private, but nothing should rely on only that.
    return (
        f'{settings.LIVEKIT_RECORDING_PREFIX}/{call.conversation_id}/'
        f'{call.pk}/{uuid.uuid4().hex}.{extension}'
    )


def start_recording(user, call_id):
    """Start recording a call.

    Two short transactions with the LiveKit call between them, so the request to
    the egress service is never made while a row lock is held. The first writes
    a STARTING row - which the unique constraint makes the only one in flight -
    and the second records what LiveKit answered.
    """
    if not livekit.recording_is_configured():
        raise CallServiceError('Recording is not available right now.')

    with transaction.atomic():
        call = _lock(call_id)
        _require_member(user, call.conversation)
        if not may_administer(user, call):
            raise PermissionDenied(
                'Only the person who started the call or a group admin can '
                'record it.'
            )

        plan = plan_for_call(call)
        if not plan.has_call_recording:
            raise PermissionDenied(
                f'The {plan.name} plan does not include call recording. '
                'Upgrade the group owner\'s plan to record calls.'
            )
        if not call.is_live:
            raise Conflict('This call has ended.')

        audio_only = call.call_type == CallRoom.CallType.AUDIO
        key = _recording_key(call, audio_only)
        try:
            with transaction.atomic():
                recording = CallRecording.objects.create(
                    call_room=call,
                    started_by=user,
                    s3_bucket=settings.LIVEKIT_RECORDING_BUCKET,
                    s3_key=key,
                    content_type='audio/ogg' if audio_only else 'video/mp4',
                )
        except IntegrityError:
            raise Conflict('This call is already being recorded.')

    try:
        info = livekit.start_recording(
            room_name=call.room_name, key=key, audio_only=audio_only
        )
    except LiveKitNotFound:
        _fail(recording, 'Nobody had connected to the call.')
        raise Conflict('Nobody has connected to this call yet, so there is nothing to record.')
    except Exception:
        # Anything else, too: a row left STARTING would refuse every later
        # attempt until the sweep noticed it.
        _fail(recording, 'LiveKit did not start the recording.')
        raise

    with transaction.atomic():
        recording = CallRecording.objects.select_for_update().get(pk=recording.pk)
        recording.egress_id = info.egress_id
        if recording.status == CallRecording.Status.STARTING:
            recording.status = CallRecording.Status.RECORDING
        recording.save(update_fields=['egress_id', 'status'])
        _record(call, CallEvent.Type.RECORDING_STARTED, user)
        _broadcast(call, RECORDING_STARTED, user_id=user.pk, recording_id=recording.pk)
    return recording


def _fail(recording, error):
    CallRecording.objects.filter(
        pk=recording.pk, status__in=CallRecording.IN_FLIGHT
    ).update(status=CallRecording.Status.FAILED, error=error, ended_at=timezone.now())


def stop_recording(user, call_id):
    """Stop the recording in progress. The file is finalised in the background."""
    with transaction.atomic():
        call = _lock(call_id)
        _require_member(user, call.conversation)
        if not may_administer(user, call):
            raise PermissionDenied(
                'Only the person who started the call or a group admin can '
                'stop recording it.'
            )

        recording = (
            CallRecording.objects.select_for_update()
            .filter(call_room=call, status__in=CallRecording.IN_FLIGHT)
            .first()
        )
        if recording is None:
            raise ValidationError('This call is not being recorded.')
        if recording.status == CallRecording.Status.STARTING:
            raise Conflict('The recording is still starting. Try again in a moment.')
        if recording.status == CallRecording.Status.STOPPING:
            return recording  # already asked; idempotent

        recording.status = CallRecording.Status.STOPPING
        recording.save(update_fields=['status'])

    try:
        info = livekit.stop_recording(recording.egress_id)
    except LiveKitNotFound:
        # LiveKit no longer has it, so it has already ended. Let the reconcile
        # decide what became of the file.
        info = None
    except CallServiceError:
        # Not stopped after all - put it back, so the person can try again and
        # the row does not claim something that did not happen.
        CallRecording.objects.filter(
            pk=recording.pk, status=CallRecording.Status.STOPPING
        ).update(status=CallRecording.Status.RECORDING)
        raise

    with transaction.atomic():
        _record(call, CallEvent.Type.RECORDING_STOPPED, user)
        _broadcast(call, RECORDING_STOPPED, user_id=user.pk, recording_id=recording.pk)

    if info is not None:
        apply_egress_info(info)
    recording.refresh_from_db()
    return recording


def apply_egress_info(info):
    """Bring a recording in line with what LiveKit says about its egress job.

    The one place a recording's state is written from LiveKit, used by the
    webhook and by the reconcile alike. Idempotent, because LiveKit retries
    webhooks and the two paths race: a recording that has reached a final state
    is never moved.
    """
    with transaction.atomic():
        recording = (
            CallRecording.objects.select_for_update()
            .filter(egress_id=info.egress_id)
            .first()
        )
        if recording is None or recording.status in (
            CallRecording.Status.COMPLETED,
            CallRecording.Status.FAILED,
            CallRecording.Status.DELETED,
        ):
            return recording

        previous = recording.status
        status = info.status
        call = recording.call_room

        if status in (lk.EgressStatus.EGRESS_STARTING, lk.EgressStatus.EGRESS_ACTIVE):
            if previous == CallRecording.Status.STARTING:
                recording.status = CallRecording.Status.RECORDING
            recording.save(update_fields=['status'])
            return recording

        if status == lk.EgressStatus.EGRESS_ENDING:
            recording.status = CallRecording.Status.STOPPING
            recording.save(update_fields=['status'])
            return recording

        recording.ended_at = timezone.now()
        result = info.file_results[0] if info.file_results else None

        if status == lk.EgressStatus.EGRESS_COMPLETE and result is not None:
            recording.status = CallRecording.Status.COMPLETED
            recording.size_bytes = result.size or None
            # LiveKit reports durations in nanoseconds.
            recording.duration_seconds = int(result.duration // 1_000_000_000)
            retention = plan_for_call(call).call_recording_retention_days
            if retention is not None:
                recording.expires_at = recording.ended_at + timedelta(days=retention)
        else:
            recording.status = CallRecording.Status.FAILED
            recording.error = (
                info.error or 'The recording produced no file.'
            )[:500]
        recording.save()

        # A recording that ended without anybody stopping it - the call ended,
        # or the egress failed - has not been announced as stopped yet.
        if previous == CallRecording.Status.RECORDING:
            _record(call, CallEvent.Type.RECORDING_STOPPED)
            _broadcast(call, RECORDING_STOPPED, recording_id=recording.pk)
    return recording


def recording_download_url(recording):
    if recording.status == CallRecording.Status.DELETED:
        raise Conflict('This recording has been deleted.')
    if recording.status != CallRecording.Status.COMPLETED:
        raise Conflict('This recording is not ready yet.')
    return storage.download_url(recording)


def delete_recording(user, recording, *, by_retention=False):
    """Delete a recording's file, keeping its row.

    The row stays so a call still says it was recorded, and when, and by whom -
    but the pointer to the file goes with the file, so nothing keeps an address
    for an object that is not there.

    The file is removed first: a row marked deleted whose file is still in the
    bucket is a recording nobody can reach and everybody is still paying for. If
    S3 fails, nothing is marked and the delete can simply be tried again.
    """
    if recording.status == CallRecording.Status.DELETED:
        return recording
    if recording.is_in_flight:
        raise Conflict('Stop the recording before deleting it.')

    if recording.s3_key:
        storage.delete_file(recording)

    recording.status = CallRecording.Status.DELETED
    recording.deleted_at = timezone.now()
    recording.deleted_by = None if by_retention else user
    recording.s3_key = ''
    recording.s3_bucket = ''
    recording.save(
        update_fields=['status', 'deleted_at', 'deleted_by', 's3_key', 's3_bucket']
    )
    return recording


def purge_expired_recordings(now=None):
    """Delete every recording whose plan's retention has run out."""
    now = now or timezone.now()
    deleted = 0
    for recording in CallRecording.objects.filter(
        status=CallRecording.Status.COMPLETED, expires_at__lte=now
    ):
        try:
            delete_recording(None, recording, by_retention=True)
            deleted += 1
        except CallServiceError:
            # Left as it is and found again tomorrow. One bad object must not
            # stop the rest of the sweep.
            logger.warning('Retention could not delete recording %s', recording.pk)
    return deleted


# -- LiveKit's webhook ---------------------------------------------------------


def _user_id(identity):
    """The Django user a LiveKit identity stands for, or None.

    Only ever a digit string - the identity WE mint. The recorder joins a room
    as a participant too, with an identity of LiveKit's own, and it is not a
    person.
    """
    return int(identity) if identity and identity.isdigit() else None


def handle_webhook(event):
    """Act on one verified LiveKit event. Unknown ones are ignored."""
    name = event.event

    if name in ('egress_started', 'egress_updated', 'egress_ended'):
        apply_egress_info(event.egress_info)
        return

    call_id = (
        CallRoom.objects.filter(room_name=event.room.name).values_list('pk', flat=True).first()
    )
    if call_id is None:
        return

    if name == 'participant_joined':
        user_id = _user_id(event.participant.identity)
        if user_id is not None:
            CallParticipant.objects.filter(
                call_room_id=call_id,
                user_id=user_id,
                left_at__isnull=True,
                connected_at__isnull=True,
            ).update(connected_at=timezone.now())

    elif name == 'participant_left':
        user_id = _user_id(event.participant.identity)
        # Somebody who reconnected (a page refresh, a second tab) makes LiveKit
        # drop the OLD session for having a duplicate identity, and says so. The
        # person is still there, on the new one - closing their stay now would
        # report them gone while they are talking.
        if user_id is None or (
            event.participant.disconnect_reason == lk.DisconnectReason.DUPLICATE_IDENTITY
        ):
            return
        user = get_user_model().objects.filter(pk=user_id).first()
        if user is None:
            return
        with transaction.atomic():
            call = _lock(call_id)
            _leave(call, user)

    elif name == 'room_finished':
        with transaction.atomic():
            call = _lock(call_id)
            if call.is_live:
                _end(call, CallRoom.EndReason.EMPTY)


# -- the sweep -----------------------------------------------------------------


def sweep_stale_calls(now=None):
    """Put right the calls that nobody ended.

    A phone loses signal, a laptop is shut, a tab is killed: none of it tells
    us. Every way a call can be left hanging is closed here:

        an unanswered one-to-one call   ends after CALL_RING_TIMEOUT_SECONDS
        a token nobody connected with   gives the place back once it expires
        a call that never ends          ends after CALL_MAX_DURATION_HOURS
        a recording that lost its       is asked about directly
        webhook

    Returns how many calls it ended.
    """
    now = now or timezone.now()
    ended = 0

    ring_cutoff = now - timedelta(seconds=settings.CALL_RING_TIMEOUT_SECONDS)
    for call_id in list(
        CallRoom.objects.filter(
            status=CallRoom.Status.RINGING,
            conversation__kind=Conversation.Kind.DIRECT,
            created_at__lt=ring_cutoff,
        ).values_list('pk', flat=True)
    ):
        ended += _sweep_end(call_id, CallRoom.EndReason.UNANSWERED, status=CallRoom.Status.RINGING)

    # Somebody who asked to join and never connected still counts against the
    # plan's cap. Once their token has expired they cannot connect at all.
    stale = list(
        CallParticipant.objects.filter(
            left_at__isnull=True,
            connected_at__isnull=True,
            token_expires_at__lt=now - STALE_STAY_GRACE,
            call_room__status__in=CallRoom.LIVE_STATUSES,
        ).values_list('call_room_id', 'user_id')
    )
    for call_id, user_id in stale:
        with transaction.atomic():
            call = _lock(call_id)
            stay = call.participants.filter(
                user_id=user_id,
                left_at__isnull=True,
                connected_at__isnull=True,
                token_expires_at__lt=now - STALE_STAY_GRACE,
            ).first()
            if stay is not None:
                was_live = call.is_live
                _leave(call, stay.user)
                ended += int(was_live and not call.is_live)

    max_cutoff = now - timedelta(hours=settings.CALL_MAX_DURATION_HOURS)
    for call_id in list(
        CallRoom.objects.filter(
            status__in=CallRoom.LIVE_STATUSES, created_at__lt=max_cutoff
        ).values_list('pk', flat=True)
    ):
        ended += _sweep_end(call_id, CallRoom.EndReason.TIMED_OUT)

    reconcile_recordings(now)
    return ended


def _sweep_end(call_id, reason, status=None):
    with transaction.atomic():
        call = _lock(call_id)
        # Re-read under the lock: somebody may have answered since the query.
        if not call.is_live or (status is not None and call.status != status):
            return 0
        _end(call, reason)
    return 1


def reconcile_recordings(now=None):
    """Ask LiveKit about recordings whose webhook never came.

    Only those that have been in flight for a while after their call ended, or
    that never got an egress id at all: a healthy recording is finished by its
    webhook within seconds, and is not worth a request.
    """
    now = now or timezone.now()
    cutoff = now - RECORDING_RECONCILE_AFTER
    lost = CallRecording.objects.filter(
        Q(status__in=CallRecording.IN_FLIGHT, call_room__ended_at__lt=cutoff)
        | Q(status=CallRecording.Status.STARTING, started_at__lt=cutoff)
    )

    for recording in lost:
        if not recording.egress_id:
            _fail(recording, 'LiveKit never confirmed the recording started.')
            continue
        try:
            info = livekit.get_recording(recording.egress_id)
        except CallServiceError:
            continue  # asked again next minute
        if info is None:
            _fail(recording, 'LiveKit has no record of this recording.')
        else:
            apply_egress_info(info)
