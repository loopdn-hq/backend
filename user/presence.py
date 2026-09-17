"""Who is online, and when they were last seen.

ONE PERSON IS SEVERAL SOCKETS. A laptop and a phone are two connections for
one human, so "offline" cannot mean "a socket closed" - it means the LAST one
did. The count lives in the cache (Redis wherever there is more than one
worker), and only the first connect and the last disconnect change anything.

PRESENCE EXPIRES, SO A CRASH CANNOT LEAVE SOMEBODY ONLINE FOREVER. A worker
that dies never runs `disconnect`, and a flag nobody clears is a green dot
beside somebody who left hours ago. So an open socket BEATS - the client pings
every `HEARTBEAT_EVERY` - and `is_online` only believes a flag whose beat is
newer than `STALE_AFTER`. Nothing has to sweep: a stale row reads as offline
on its own, and corrects itself the moment that person connects again.

WRITTEN WITH `update()`, NEVER `save()`. `Profile.save` carries the phone
verification rule, and presence must not be able to trip it.

WHAT OTHER PEOPLE SEE IS `PrivacySettings.online_status_visible`, asked in
`user/privacy.py` - here for the broadcast, and in the serializer for a fetch.
"""
import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

#: How often an open socket says it is still there. The client's ping
#: interval; `chat/consumers.py` answers it with `beat`.
HEARTBEAT_EVERY = timedelta(seconds=60)

#: Presence older than this is not believed. Comfortably more than two missed
#: beats, so an ordinary hiccup does not blink somebody offline, and short
#: enough that a crashed worker's dot goes within a few minutes.
STALE_AFTER = timedelta(minutes=3)

#: The socket counter is a cache of something the sockets themselves know, so
#: it is allowed to expire - a count that somehow outlives its sockets would
#: hold somebody online, and `STALE_AFTER` would then be the only thing
#: correcting it.
_COUNTER_TTL = int(timedelta(minutes=30).total_seconds())


def _counter_key(user_id):
    return f'presence:sockets:{user_id}'


def _beat_key(user_id):
    return f'presence:beat:{user_id}'


def connected(user):
    """Record one more open socket. True when it is this person's first.

    The caller broadcasts on a True, and only then: a second device arriving
    changes nothing anybody needs to hear about.
    """
    key = _counter_key(user.pk)
    cache.add(key, 0, _COUNTER_TTL)
    try:
        count = cache.incr(key)
    except ValueError:
        # The key expired between `add` and `incr`. One socket, then.
        cache.set(key, 1, _COUNTER_TTL)
        count = 1

    _write(user, online=True)
    return count == 1


def disconnected(user):
    """Record one socket closing. True when it was this person's last."""
    key = _counter_key(user.pk)
    try:
        count = cache.decr(key)
    except ValueError:
        # Nothing to decrement - a restart lost the count. Treat it as the
        # last one: a dot that goes out early is better than one that stays.
        count = 0

    if count > 0:
        return False

    cache.delete(key)
    _write(user, online=False)
    return True


def beat(user):
    """An open socket saying it is still there.

    Throttled to one write per `HEARTBEAT_EVERY`, because this runs for every
    socket of every signed-in person and the row it touches is read by every
    member list. The cache key expiring IS the throttle.
    """
    key = _beat_key(user.pk)
    if not cache.add(key, 1, int(HEARTBEAT_EVERY.total_seconds())):
        return False
    _write(user, online=True)
    return True


def _write(user, *, online):
    from user.models import Profile

    Profile.objects.filter(user=user).update(
        is_online=online, last_seen_at=timezone.now()
    )


def is_online(profile):
    """Whether this profile counts as online RIGHT NOW.

    The flag alone is not enough: it is only as good as the last beat behind
    it (see the module docstring).
    """
    if profile is None or not profile.is_online or profile.last_seen_at is None:
        return False
    return timezone.now() - profile.last_seen_at <= STALE_AFTER


def frames(user, *, online):
    """Who to tell that this person's presence changed, and what to send.

    Returns `(groups, payload)` rather than sending: this is database work,
    and the caller is a consumer whose event loop is waiting for it. Sending
    from in here would mean `async_to_sync` inside `database_sync_to_async` -
    the send waits for the loop, the loop waits for the database thread, and
    the socket hangs. Found exactly that way.

    Their own conversations' groups, so exactly the people who can already see
    them in a member list hear it - and nobody else.

    Nothing at all for anybody who turned Online status off: with it off there
    is nothing to tell, and sending it "for the client to filter" would be the
    leak itself.
    """
    from chat.models import Participant
    from chat.services import conversation_group
    from user import privacy

    if not privacy.shows_online_status(user):
        return [], None

    groups = [
        conversation_group(conversation_id)
        for conversation_id in set(
            Participant.objects.filter(
                user=user, status=Participant.Status.ACTIVE
            ).values_list('conversation_id', flat=True)
        )
    ]
    payload = {
        'type': 'presence',
        'user_id': user.pk,
        'online': online,
        'last_seen_at': timezone.now().isoformat(),
    }
    return groups, payload
