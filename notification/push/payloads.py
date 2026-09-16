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


def for_notification(notification):
    """One activity event - today, being added to a Loop.

    The event's own words, not the message templates above: nothing was said,
    somebody did something. `show_preview` still applies, because the name of a
    Loop somebody was added to is as much theirs as a message is.
    """
    conversation = notification.conversation
    group_name = (getattr(conversation, 'name', '') or '').strip() or 'a Loop'
    actor = _actor_label(notification)

    if rules.shows_preview(notification.to_user):
        body = f'{actor} added you to {group_name}'
    else:
        body = 'You were added to a Loop'

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


def _clip(text):
    text = (text or '').strip()
    if len(text) <= BODY_CHARS:
        return text
    return text[:BODY_CHARS].rstrip() + '…'
