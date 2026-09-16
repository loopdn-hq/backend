"""Recording the things that happen to people, so they survive not looking.

Every event here already existed - it was broadcast to a socket and then gone.
A member who was promoted while their laptop was shut simply never found out.
This is the durable half: one row per event per recipient, and the same socket
frame on top so an open client updates without polling.

One entry point, `notify`, for the same reason chat/membership.py has one entry
point per action: the REST API, the inbound-SMS commands and a Celery sweep all
have to produce identical rows, and three copies of that would be three places
to get the recipient wrong.

Never called for a new message. `Participant.last_read_message` and the
`unread_count` annotation already answer "is there anything new here", and a
second answer that could disagree is worse than no second answer.
"""
import logging

from django.db import transaction

from notification.models import Notification

logger = logging.getLogger(__name__)


def notify(to_user, type, *, from_user=None, conversation=None, message=None,
           data=None):
    """Record one event for one person, and push it to their open clients.

    Silently does nothing when `to_user` is None, because most callers are
    working from a Participant and a phone-only member has no account to
    notify - making every call site check that itself is how one of them ends
    up not checking.

    Never notifies somebody about their own action: being told "you promoted
    yourself" is noise, and the actor already saw the result.
    """
    if to_user is None:
        return None
    if from_user is not None and from_user.pk == to_user.pk:
        return None

    notification = Notification.objects.create(
        to_user=to_user,
        from_user=from_user,
        type=type,
        conversation=conversation,
        message=message,
        data=data or None,
    )

    # After commit: a client that reacts by refetching must not race the write,
    # and a rolled-back transaction must not leave a notification behind for an
    # event that never happened.
    transaction.on_commit(lambda: _push(notification))
    return notification


def notify_many(users, type, **kwargs):
    """The same, for the several people one event concerns - a group's admins.

    Deduplicated on the way in: an admin who is also the actor is dropped by
    `notify`, and the same user appearing twice in the queryset would otherwise
    get two identical rows.
    """
    seen = set()
    created = []
    for user in users:
        if user is None or user.pk in seen:
            continue
        seen.add(user.pk)
        row = notify(user, type, **kwargs)
        if row is not None:
            created.append(row)
    return created


def _push(notification):
    """Put a new notification on the recipient's own socket.

    Their own channel, never the conversation's: this is one person's activity
    feed, and the conversation group would hand it to everybody in the group.

    The socket and its groups are chat's (chat/services.py, and
    `notification_new` in chat/consumers.py); this only puts the frame on it.
    """
    from chat.services import send_to_group, user_group
    from notification.serializers import NotificationSerializer

    send_to_group(
        user_group(notification.to_user_id),
        {
            'type': 'notification.new',
            'notification': NotificationSerializer(notification).data,
        },
    )

    # And to their browsers, for the kinds of event they asked to be told
    # about - see notification/push/rules.py. Caught here: the socket frame
    # above has already gone out, and a push failing must not undo it or
    # bubble into whatever created the notification.
    #
    # A function-local import because push reads this app's models and the
    # user's settings, which are not ready at module import time.
    from notification.push.outbound import schedule_notification

    try:
        schedule_notification(notification)
    except Exception:
        logger.exception('Push failed for notification %s', notification.pk)
