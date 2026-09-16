"""What a notification says, and where it goes when tapped.

Every string a person reads on their lock screen is built here, so "Show sender
and message" is honoured in one place rather than in each kind of alert. Get
that wrong in one branch and somebody's private message is on a desk for a
passer-by to read - which is the whole reason the switch exists.

The payload is JSON, encrypted to the browser's own keys, and read by
`public/sw.js` in the web app. Its shape is a contract with that file:

    {title, body, tag, silent, url, data: {conversation_id, message_id, kind}}
"""
from chat.models import Conversation
from notification.models import Notification
from notification.push import rules

#: Long enough to be worth reading, short enough that no platform truncates it
#: somewhere embarrassing. Android shows roughly this much on one line.
BODY_CHARS = 140

#: What an alert says when the person has "Show sender and message" off. One
#: phrase per kind, none of them naming anybody or quoting anything.
WITHHELD = {
    rules.MENTION: 'Somebody mentioned you',
    rules.REPLY: 'Somebody replied to you',
    rules.GROUP: 'New message in one of your Loops',
    rules.DIRECT: 'New direct message',
}

PRODUCT = "Loopd'n"


def for_message(message, participant, kind):
    """One message, as the recipient's browser will show it.

    `kind` comes from `rules.message_alert` and decides the sentence: being
    named reads differently from an ordinary message in a busy group, and that
    difference is most of why somebody leaves mentions on and group messages
    off.
    """
    user = participant.user
    conversation = message.conversation
    is_group = conversation.kind == Conversation.Kind.GROUP
    group_name = (conversation.name or '').strip() or 'A Loop'

    if not rules.shows_preview(user):
        title, body = PRODUCT, WITHHELD[kind]
    else:
        sender = _sender_label(message)
        text = _clip(message.preview_text)

        if kind == rules.MENTION:
            title = group_name if is_group else sender
            body = f'{sender} mentioned you: {text}'
        elif kind == rules.REPLY:
            title = group_name if is_group else sender
            body = f'{sender} replied: {text}'
        elif is_group:
            title = group_name
            body = f'{sender}: {text}'
        else:
            # A direct message names the person in the title, so repeating them
            # in the body would read as "Alice: Alice said".
            title, body = sender, text

    return {
        'title': title,
        'body': body,
        # One live notification per conversation. Without a tag, a group having
        # a conversation stacks twenty alerts somebody has to clear one by one.
        'tag': f'conversation-{conversation.pk}',
        'silent': rules.is_silent(user),
        'url': url_for_conversation(conversation.pk),
        'data': {
            'conversation_id': conversation.pk,
            'message_id': message.pk,
            'kind': kind,
        },
    }


#: What each pushed event says: (with the preview on, with it off).
#:
#: One entry per type in `rules.SETTING_BY_NOTIFICATION_TYPE`, and a test holds
#: the two in step - a type that is pushed with no words here would arrive as a
#: blank notification, which is the one failure nobody would report because it
#: looks like the feature simply did not fire.
#:
#: The withheld half names nobody and no group, for the phone face-up on a desk
#: that a passer-by can read.
EVENT_WORDS = {
    Notification.Type.MEMBER_ADDED: (
        '{actor} added you to {group}',
        'You were added to a Loop',
    ),
    Notification.Type.JOIN_APPROVED: (
        '{actor} approved your request to join {group}',
        'Your request to join a Loop was approved',
    ),
    # This one goes to the group's ADMINS, not to the person asking - so it is
    # written from the admin's side.
    Notification.Type.JOIN_REQUESTED: (
        '{actor} asked to join {group}',
        'Somebody asked to join your Loop',
    ),
    # These two go to the SENDER, about their own message. `{people}` is
    # "1 person" or "4 people" - see `_people`.
    Notification.Type.SMS_SEND_FAILED: (
        '{people} did not get your message to {group} by text',
        'A message you sent did not reach everyone',
    ),
    Notification.Type.EMAIL_SEND_FAILED: (
        '{people} did not get your message to {group} by email',
        'A message you sent did not reach everyone',
    ),
}


def for_notification(notification):
    """One activity event: getting into a Loop, or being asked about it.

    The event's own words, not the message templates above - nothing was said,
    somebody did something - so each type has its own sentence in EVENT_WORDS.
    `show_preview` still applies: the name of a Loop, and who put you in it, is
    as much somebody's business as a message is.

    A type with no entry cannot arrive here: `rules.notification_alert` refuses
    anything that is not in its map, and the two are held in step by a test.
    """
    conversation = notification.conversation
    group_name = (getattr(conversation, 'name', '') or '').strip() or 'a Loop'
    actor = _actor_label(notification)

    shown, withheld = EVENT_WORDS[notification.type]
    if rules.shows_preview(notification.to_user):
        body = shown.format(
            actor=actor,
            group=group_name,
            people=_people((notification.data or {}).get('count', 0)),
        )
    else:
        body = withheld

    return {
        'title': PRODUCT,
        'body': body,
        'tag': f'activity-{notification.pk}',
        'silent': rules.is_silent(notification.to_user),
        'url': (
            url_for_conversation(conversation.pk)
            if conversation is not None
            else '/activity/'
        ),
        'data': {
            'conversation_id': getattr(conversation, 'pk', None),
            'notification_id': notification.pk,
            'kind': notification.type,
        },
    }


def url_for_conversation(conversation_id):
    """Where a tap lands.

    A hash rather than a path or a query: the web app is a static export whose
    conversation is client state, and `useSearchParams` on a prerendered route
    would force a Suspense boundary (see AppShellProvider). The shell reads
    this hash and opens that conversation, the same way /settings/ reads its
    own.
    """
    return f'/chats/#conversation={conversation_id}'


def _sender_label(message):
    """Who to name, account or not.

    `sender_participant.display_label` rather than `sender_label`, which is a
    USERNAME - fine in a log, wrong on a lock screen, where somebody expects
    the name they see in the app.
    """
    participant = message.sender_participant
    if participant is not None:
        return participant.display_label
    return message.sender_label


def _actor_label(notification):
    actor = notification.from_user
    if actor is None:
        return 'Somebody'
    profile = getattr(actor, 'profile', None)
    return getattr(profile, 'display_label', None) or actor.username


def _people(count):
    """"1 person" or "4 people" - the difference somebody reads first."""
    return '1 person' if count == 1 else f'{count} people'


def _clip(text):
    text = (text or '').strip()
    if len(text) <= BODY_CHARS:
        return text
    return text[:BODY_CHARS].rstrip() + '…'
