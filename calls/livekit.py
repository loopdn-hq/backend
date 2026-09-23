"""The one place Django talks to LiveKit.

LiveKit is a separate, self-hosted service and carries all of the audio and
video. Django never sees a media byte: it decides who may be in a call, signs a
short-lived token that lets a browser connect to LiveKit directly, and (for
recording and ending a call) makes a few control calls to LiveKit's server API.

Everything the rest of the app needs from the SDK is a function here, so that

    - the SDK is imported in one module, and a test patches one module rather
      than the SDK's internals;
    - the SDK is asynchronous and Django's views are not, and this is the only
      place that has to know how to bridge the two;
    - a provider failure becomes one exception the API maps to a 503, in the
      way `SmsError` and `EmailError` are mapped.

The API secret is read from settings and signs tokens here. It is never
returned, logged or put in an exception message.
"""
import asyncio
import logging
from datetime import timedelta

import aiohttp
from django.conf import settings
from livekit import api as lk

logger = logging.getLogger(__name__)

# The publishing sources a token may grant. Named by the SDK's own strings.
MICROPHONE = 'microphone'
CAMERA = 'camera'
SCREEN_SHARE = 'screen_share'
SCREEN_SHARE_AUDIO = 'screen_share_audio'

# LiveKit's Twirp error code for "there is no such room / egress".
NOT_FOUND = 'not_found'


class CallServiceError(Exception):
    """A service the calling feature depends on failed - LiveKit, or S3.

    The API answers these with a 503, as it does `SmsError` and `EmailError`:
    the request was fine and the provider was not. The message is written for
    the person who asked, so it says what to do (try again), never what went
    wrong inside.
    """


class LiveKitError(CallServiceError):
    """LiveKit could not be reached, or refused."""


class LiveKitNotFound(LiveKitError):
    """LiveKit has no such room or recording."""


class LiveKitNotConfigured(LiveKitError):
    """No LiveKit credentials here - a laptop, or a deploy that forgot them."""


def is_configured():
    return bool(
        settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET
    )


def require_configured():
    """Raise before any row is written for a call that could never be joined."""
    if not is_configured():
        raise LiveKitNotConfigured('Calling is not available right now.')


def recording_is_configured():
    return is_configured() and bool(settings.LIVEKIT_RECORDING_BUCKET)


# -- tokens ------------------------------------------------------------------


def mint_token(*, room_name, identity, name, sources):
    """A participant token: permission to join ONE room, for a few minutes.

    Signed locally with the API secret - no network call, so this cannot fail
    for LiveKit being down, and cannot be slow.

    `identity` is what the other participants see the person as and what
    LiveKit reports back in its webhooks, so it is the Django user's id and
    nothing else. `sources` is what they may publish, decided by the caller
    from the call type and the plan; a client cannot ask for more, because the
    grant is inside the signed token.

    Deliberately NOT granted: room_create, room_admin, room_record, hidden and
    everything else that would let a participant do more than take part.
    """
    require_configured()

    ttl = timedelta(seconds=settings.LIVEKIT_TOKEN_TTL_SECONDS)
    return (
        lk.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(str(identity))
        .with_name(name)
        .with_ttl(ttl)
        .with_grants(
            lk.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
                can_publish_sources=list(sources),
            )
        )
        .to_jwt()
    )


# -- server API --------------------------------------------------------------


def _run(operation):
    """Run one async SDK call from synchronous code.

    A fresh client per call, because it owns an HTTP session that belongs to one
    event loop and a loop lives only as long as `asyncio.run`. These calls are
    rare (ending a call, starting or stopping a recording), so the cost of a
    new session is not worth a pool that would have to survive that.

    Safe from a view or a Celery task, neither of which has a running loop in
    its thread. Do not call this from `async def` code.
    """
    require_configured()

    async def go():
        client = lk.LiveKitAPI(
            settings.LIVEKIT_API_URL,
            settings.LIVEKIT_API_KEY,
            settings.LIVEKIT_API_SECRET,
            # Bounded, because with no Celery worker this runs on the request
            # thread: an unreachable LiveKit must fail in seconds, not hold
            # a leave or an end for the SDK's default.
            timeout=aiohttp.ClientTimeout(total=settings.LIVEKIT_API_TIMEOUT_SECONDS),
        )
        try:
            return await operation(client)
        finally:
            await client.aclose()

    try:
        return asyncio.run(go())
    except LiveKitError:
        raise
    except Exception as exc:
        if getattr(exc, 'code', '') == NOT_FOUND:
            # The caller decides whether "no such thing" matters: for closing a
            # room it does not, for starting a recording it does.
            raise LiveKitNotFound('There is no such call in progress.') from exc
        if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)):
            # Cannot get to LiveKit at all - a wrong or not-yet-created address,
            # a server that is down, a firewall. One line, without the
            # traceback: the cause is in the message, and a trace through
            # aiohttp's internals only buries it. Not the secret, which is never
            # in a URL.
            logger.error(
                'Cannot reach LiveKit at %s (%s: %s). Check LIVEKIT_URL / '
                'LIVEKIT_API_URL and that the server is running.',
                settings.LIVEKIT_API_URL, type(exc).__name__, exc,
            )
        else:
            # Anything else is unexpected, and worth the traceback.
            logger.exception('LiveKit request failed')
        raise LiveKitError(
            'The call service is not responding. Try again in a moment.'
        ) from exc


def delete_room(room_name):
    """Close a room, which disconnects everybody still in it.

    A room that does not exist is fine: it means nobody ever connected, or
    LiveKit already closed it, and either way the call is over.
    """
    try:
        _run(lambda client: client.room.delete_room(lk.DeleteRoomRequest(room=room_name)))
    except LiveKitNotFound:
        pass


def start_recording(*, room_name, key, audio_only):
    """Start recording a room to S3. Returns LiveKit's EgressInfo.

    LiveKit's egress service composes the room into one file and uploads it
    itself, straight to `key` in the recording bucket - nothing passes through
    Django.
    """
    s3 = lk.S3Upload(
        bucket=settings.LIVEKIT_RECORDING_BUCKET,
        region=settings.LIVEKIT_RECORDING_REGION,
    )
    # Only when given: blank means the egress service uses the credentials it
    # runs with, which is what production should do.
    if settings.LIVEKIT_RECORDING_ACCESS_KEY and settings.LIVEKIT_RECORDING_SECRET:
        s3.access_key = settings.LIVEKIT_RECORDING_ACCESS_KEY
        s3.secret = settings.LIVEKIT_RECORDING_SECRET

    output = lk.EncodedFileOutput(
        file_type=lk.EncodedFileType.OGG if audio_only else lk.EncodedFileType.MP4,
        filepath=key,
        s3=s3,
    )
    request = lk.RoomCompositeEgressRequest(
        room_name=room_name,
        layout='speaker',
        audio_only=audio_only,
        file_outputs=[output],
    )
    return _run(lambda client: client.egress.start_room_composite_egress(request))


def stop_recording(egress_id):
    """Ask LiveKit to finish a recording. Returns its EgressInfo."""
    return _run(
        lambda client: client.egress.stop_egress(lk.StopEgressRequest(egress_id=egress_id))
    )


def get_recording(egress_id):
    """LiveKit's current view of one recording, or None if it has none."""
    try:
        response = _run(
            lambda client: client.egress.list_egress(
                lk.ListEgressRequest(egress_id=egress_id)
            )
        )
    except LiveKitNotFound:
        return None
    return response.items[0] if response.items else None


# -- webhooks ----------------------------------------------------------------


def parse_webhook(body, authorization):
    """Verify a LiveKit webhook and return its event.

    LiveKit signs each delivery: the Authorization header is a JWT made with the
    same API key and secret, carrying a hash of the body. Checking both is what
    makes an endpoint that anybody on the internet can reach safe to act on.

    Raises LiveKitError for anything that does not verify, without saying why.
    """
    if not (settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET):
        raise LiveKitNotConfigured('Calling is not available right now.')

    receiver = lk.WebhookReceiver(
        lk.TokenVerifier(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
    )
    try:
        return receiver.receive(body, authorization)
    except Exception as exc:
        raise LiveKitError('Webhook could not be verified.') from exc
