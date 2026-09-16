"""Whether this person wants to be told about this, and as what.

THE ONLY PLACE THAT READS NotificationSettings
----------------------------------------------
Every switch on Settings -> Notifications is answered here and nowhere else.
The screen saves eleven booleans; what they MEAN is one function, so "does
Mentions & replies override Group messages" has exactly one answer and it is
written down next to the reason.

The rest of the path asks a different question and has already answered it
before anything here is called: `chat/delivery.py:membership_reason` says
whether this person is still in the group and whether they muted it, and the
block screen says whether they will hear about this sender at all. Those are
facts about membership; these are preferences about alerts, and mixing the two
is how a muted group starts buzzing because somebody used the word everyone.

THE LADDER, IN ORDER
--------------------
    messages off              -> nothing, whatever kind it is
    named or answered you     -> DELIVER when Mentions & replies is on
    a group message           -> DELIVER when Group messages is on
    a direct message          -> DELIVER when Direct messages is on

"Named or answered you" is asked ABOVE the other two on purpose (Yul's call):
somebody who turned Group messages off in a busy Loop still wants to know when
a message is addressed to them personally - it is how Slack and WhatsApp
behave, and the alternative makes the Mentions row do nothing for exactly the
people most likely to switch the others off. Turning "Messages" off silences
all four, and muting the Loop silences it earlier still.
"""
import logging

from chat.models import Conversation
from notification.models import Notification
from user.models import NotificationSettings

logger = logging.getLogger(__name__)

#: Which switch governs which activity event. A map rather than a chain of ifs
#: so that a new notification type joins it in one line - and so that a type
#: NOT in it is silently never pushed, which is the safe default for the eight
#: types that have no switch on the screen (join requests, role changes, SMS
#: opt-outs: all of them already appear in the activity feed).
SETTING_BY_NOTIFICATION_TYPE = {
    Notification.Type.MEMBER_ADDED: 'group_invitations',
}

#: The kinds of message alert, which are also what the payload is built from.
MENTION = 'mention'
REPLY = 'reply'
GROUP = 'group'
DIRECT = 'direct'


def preferences_for(user):
    """This person's switches, or the defaults when they have no row yet.

    An UNSAVED instance for the missing case, never `for_user`: this runs once
    per recipient inside a fan-out, and `get_or_create` there would be a write
    per member of every group that contains somebody who has not signed in
    since the settings models landed. The defaults are the same either way -
    everything on - and their row is created the next time they sign in.
    """
    try:
        return user.notification_settings
    except NotificationSettings.DoesNotExist:
        return NotificationSettings()


def message_alert(participant, message, *, mentioned=False, replied_to=False):
    """What to push this member about this message, or why not.

    Returns `(kind, reason)`: exactly one of the two is None. `kind` is one of
    MENTION, REPLY, GROUP or DIRECT and decides how the notification reads;
    `reason` is a short phrase for the log, in the same idiom as the delivery
    ladder's refusals, because "why did my phone not buzz" is the question this
    code exists to be able to answer.

    `mentioned` and `replied_to` are passed in rather than worked out here:
    they are facts about the message that `chat/mentions.py` owns, and asking
    for them per recipient inside this function would be a query per member.
    """
    prefs = preferences_for(participant.user)

    if not prefs.messages:
        return None, 'messages off'

    if mentioned or replied_to:
        if prefs.mentions_replies:
            return (MENTION if mentioned else REPLY), None
        # Not a refusal yet. Switching off "Mentions & replies" asks for no
        # SPECIAL treatment of a message that names you - it does not take away
        # the ordinary alert they would have got for it anyway, so the question
        # carries on down the ladder.

    if message.conversation.kind == Conversation.Kind.GROUP:
        if not prefs.group_messages:
            return None, 'group messages off'
        return GROUP, None

    if not prefs.direct_messages:
        return None, 'direct messages off'
    return DIRECT, None


def notification_alert(notification):
    """Whether to push this activity event, or why not.

    The recipient is `notification.to_user`, so unlike `message_alert` there is
    no participant here: being added to a Loop is a thing that happened to an
    ACCOUNT, and the membership it created is the subject rather than the
    filter.
    """
    field = SETTING_BY_NOTIFICATION_TYPE.get(notification.type)
    if field is None:
        return False, f'{notification.type} is not a push'

    prefs = preferences_for(notification.to_user)
    if not getattr(prefs, field):
        return False, f'{field} off'
    return True, None


def shows_preview(user):
    """Whether this person's notifications may name the sender and the words.

    Off means the alert says only that something arrived - for the phone lying
    face-up on a desk that anybody walking past can read.
    """
    return preferences_for(user).show_preview


def is_silent(user):
    """Whether their notifications should arrive without a sound.

    The Sound row offers "Loopd'n default" and "None". A browser will not play
    a file of our choosing - the `sound` member of the Notification API is
    unimplemented everywhere - so "default" means the sound the operating
    system makes for a notification, and "None" is the only one we can really
    honour: it becomes `silent: true` in the payload.
    """
    return preferences_for(user).sound == NotificationSettings.Sound.NONE
