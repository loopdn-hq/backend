"""Telling somebody their message did not get out.

ONE ALERT PER MESSAGE, PER CHANNEL - not one per person who missed it (Yul,
2026-09-16). A two-hundred-member Loop with four dead numbers would otherwise
buzz the sender four times for one message, and again for the next one: the
alert would become the reason people turn alerts off. So the count goes in the
row - "4 people did not get your message" - and who they were stays in the
delivery log, which is the thing that can answer per person.

WHO IS TOLD. The sender, and nobody else. An admin cannot fix a number that has
been disconnected, and the person who wrote the message is the one who needs to
know it did not arrive - they are the one who will otherwise assume it did.

WHEN. Only once a channel has given up: a permanent reason, too many attempts,
or past its retry window. Announcing the first failure would cry wolf, because
the retry sweep quietly fixes most of them minutes later. Each channel's
maintenance module owns that decision, next to the retry query it mirrors.

The alert is created once and its count kept current afterwards, so a second
failure on the same message updates the row rather than interrupting again.
"""
import logging

from notification.models import Notification
from notification.services import notify

logger = logging.getLogger(__name__)


def announce_failure(message, type, count):
    """Tell this message's sender that `count` people did not get it.

    Returns the Notification when something was actually said - the alert was
    created, or its count went up - and None otherwise: when there is nobody to
    tell (a message texted or emailed in by somebody with no account has no
    `sender`), or when the alert already stands unchanged.

    Idempotent per (message, type): the sweep calls this for as long as the
    failed rows are in scope, which is a day, so it must neither duplicate the
    alert nor report it as news every five minutes.
    """
    if message is None or message.sender_id is None or count < 1:
        return None

    existing = (
        Notification.objects.filter(
            to_user_id=message.sender_id, message=message, type=type
        )
        .order_by('id')
        .first()
    )

    if existing is not None:
        # More of them failed since. Updated quietly rather than notified
        # again: the sender already knows this message had trouble, and a
        # second buzz for the same message says nothing new.
        if (existing.data or {}).get('count') == count:
            # Nothing has changed since the last sweep.
            return None

        existing.data = {**(existing.data or {}), 'count': count}
        existing.save(update_fields=['data', 'updated_at'])
        return existing

    return notify(
        message.sender,
        type,
        conversation=message.conversation,
        message=message,
        # No `from_user`: nobody did this, a channel failed.
        data={'count': count},
    )


def announce_all(failures, type):
    """Announce a sweep's worth of failures, one alert per message.

    `failures` is an iterable of `(message, count)`. Each is caught on its own,
    because this runs from a scheduled job over every message that had trouble
    in the last day: one unexpected row must not stop the rest being reported.
    """
    announced = 0
    for message, count in failures:
        try:
            if announce_failure(message, type, count) is not None:
                announced += 1
        except Exception:
            logger.exception(
                'Could not announce %s failures for message %s',
                type, getattr(message, 'pk', None),
            )
    return announced
