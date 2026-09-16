"""Sending a notification to somebody's browsers.

WHAT THIS CHANNEL IS NOT
------------------------
SMS and email deliver the MESSAGE: somebody who is not in the app still reads
what was said, so every send is billed, logged per recipient, retried when it
fails and re-decided before each retry. Push delivers a TAP ON THE SHOULDER.
The message is already in the app, already broadcast to every open socket, and
already counted as unread; this only says "there is something to look at".

That difference is why there is no PushDelivery table and no retry sweep:

    no log      a row per recipient per message would be the largest table in
                the product within a week, to record something that costs
                nothing and that the unread count already answers
    no retry    a notification that arrives ten minutes late is worse than one
                that never arrives - the person has read the message by then,
                and the buzz is just noise. The browser's own push service
                already queues and retries for the seconds that matter.

What IS kept is the subscription and the reason a send was skipped, in the log,
because "my phone did not buzz" is the only support question this code
produces.
"""
import json
import logging

from django.conf import settings
from django.utils import timezone

from notification.push import payloads, rules
from notification.models import PushSubscription
from utils.dispatch import after_commit, run_somewhere

logger = logging.getLogger(__name__)


def sending_enabled():
    """Whether push is configured well enough to send anything.

    Both keys, not just the switch: VAPID is a signature the browser's push
    service verifies, and without the private key every send is rejected. Asked
    before any work is scheduled so a deployment with push off queues nothing
    at all - see notification/checks.py, which refuses a deploy that is half
    configured.
    """
    return bool(
        settings.PUSH_SENDING_ENABLED
        and settings.VAPID_PRIVATE_KEY
        and settings.VAPID_PUBLIC_KEY
    )


# --- new messages ----------------------------------------------------------


def schedule(message):
    """Queue this message's notifications for after the transaction commits.

    Registered as the `push` channel in notification/apps.py, so this is
    called for every message the product stores - including direct messages, which is the
    one way this channel differs from SMS and email (see
    chat/delivery.py:register_channel).
    """
    if not sending_enabled():
        return

    message_id = message.pk
    after_commit(
        f'push fan-out for message {message_id}',
        queue=lambda: _queue_message(message_id),
        run_here=lambda: dispatch(message_id),
    )


def dispatch(message_id):
    """Load the message with everything the fan-out reads, and fan it out."""
    from chat.models import Message

    message = (
        Message.objects.filter(pk=message_id)
        .select_related(
            'conversation',
            'sender_participant__user__profile',
            'sender',
            # Whose message this answers, which is half of "Mentions & replies".
            'reply_to',
        )
        # Who it names, read once per message rather than once per recipient.
        .prefetch_related('mentions', 'attachments')
        .first()
    )
    if message is None:
        return []
    return fan_out(message)


def fan_out(message):
    """Notify everybody in the conversation who wants to hear about this one.

    Returns the subscriptions that were really sent to, which is what the task
    logs. The order of the questions is the order of the rules, and each one
    belongs to whoever owns it:

        chat/delivery.py    still a member, not muted, not blocked
        chat/mentions.py    were they named, or answered
        push/rules.py       do their switches allow this kind of alert
        here                have they got a browser to send to
    """
    from chat import delivery, mentions

    if message.is_deleted:
        return []

    rows, screen = delivery.audience(
        message,
        select_related=('user__profile', 'user__notification_settings'),
        prefetch_related=('user__push_subscriptions',),
    )

    sent = []
    for participant in rows:
        if participant.user_id is None:
            # A phone-only member. They already get the message itself by text;
            # there is no browser here to tap on the shoulder.
            continue

        reason = delivery.membership_reason(participant) or delivery.block_reason(
            participant, screen
        )
        if reason is None:
            kind, reason = rules.message_alert(
                participant,
                message,
                mentioned=mentions.mentions_participant(message, participant),
                replied_to=mentions.is_reply_to(message, participant),
            )

        if reason is not None:
            logger.debug(
                'No push to %s for message %s: %s',
                participant.user_id, message.pk, reason,
            )
            continue

        sent += send_to_user(
            participant.user, payloads.for_message(message, participant, kind)
        )

    return sent


# --- activity events -------------------------------------------------------


def schedule_notification(notification):
    """Push one activity event, if it is one of the kinds people chose.

    Registered as a listener in notification/apps.py and called by
    `notification/services.py:notify` from inside its own on_commit - so this
    is already past the commit and asks for no second one.
    """
    if not sending_enabled():
        return

    allowed, reason = rules.notification_alert(notification)
    if not allowed:
        logger.debug(
            'No push for notification %s: %s', notification.pk, reason
        )
        return

    notification_id = notification.pk
    run_somewhere(
        f'push for notification {notification_id}',
        queue=lambda: _queue_notification(notification_id),
        run_here=lambda: dispatch_notification(notification_id),
    )


def dispatch_notification(notification_id):
    """Load one activity event and send it to its recipient's browsers."""
    from notification.models import Notification

    notification = (
        Notification.objects.filter(pk=notification_id)
        .select_related(
            'to_user__notification_settings',
            'from_user__profile',
            'conversation',
        )
        .prefetch_related('to_user__push_subscriptions')
        .first()
    )
    if notification is None:
        return []

    # Asked again here, not only in `schedule_notification`: this is also the
    # entry point when a worker picks the job up, and a switch can have been
    # turned off in between.
    allowed, reason = rules.notification_alert(notification)
    if not allowed:
        logger.debug('No push for notification %s: %s', notification_id, reason)
        return []

    return send_to_user(
        notification.to_user, payloads.for_notification(notification)
    )


# --- the wire --------------------------------------------------------------


def send_to_user(user, payload):
    """Send one payload to every browser this person has registered.

    All of them, not the newest: somebody signed in on a laptop and a phone
    expects both to buzz, and picking one would look like a bug on whichever
    device lost.
    """
    sent = []
    for subscription in user.push_subscriptions.all():
        if send(subscription, payload):
            sent.append(subscription)
    return sent


def send(subscription, payload):
    """One encrypted notification to one browser. True when it was accepted.

    Nothing raised here escapes. A fan-out is a loop over people, and one
    browser that has been wiped must not stop the rest of a group being told.
    """
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription.subscription_info,
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            # A FRESH dict per call: pywebpush adds `exp` and `aud` to whatever
            # it is handed, so a shared one would carry the first call's
            # expiry - and its audience - to a different push service.
            vapid_claims={'sub': settings.VAPID_SUBJECT},
            ttl=settings.PUSH_TTL_SECONDS,
        )
    except WebPushException as error:
        _handle_failure(subscription, error)
        return False
    except Exception:
        logger.exception(
            'Push to subscription %s failed unexpectedly', subscription.pk
        )
        return False

    # Cheap, and it is the only answer to "is this device still being used".
    PushSubscription.objects.filter(pk=subscription.pk).update(
        last_used_at=timezone.now()
    )
    return True


def _handle_failure(subscription, error):
    """Delete a subscription the push service says is gone; log the rest.

    404 and 410 are the browser's way of saying this address is dead - the site
    data was cleared, the browser uninstalled, the permission revoked. Keeping
    the row would mean paying for that lookup on every message forever, so it
    goes at the first refusal. Anything else is a bad minute at the provider or
    a mistake of ours, and deleting on those would quietly unsubscribe people
    for an outage.
    """
    status = getattr(getattr(error, 'response', None), 'status_code', None)

    if status in (404, 410):
        logger.info(
            'Push subscription %s is gone (%s); removing it',
            subscription.pk, status,
        )
        PushSubscription.objects.filter(pk=subscription.pk).delete()
        return

    logger.warning(
        'Push to subscription %s refused with %s: %s',
        subscription.pk, status, error,
    )


def _queue_message(message_id):
    from notification.tasks import fan_out_message

    fan_out_message.delay(message_id)


def _queue_notification(notification_id):
    from notification.tasks import push_notification

    push_notification.delay(notification_id)
