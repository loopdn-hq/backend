"""Settings → Notifications."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class NotificationSettings(UserSettings):
    """What a person is notified about, and how.

    Account-wide switches, read by ONE place: `notification/push/rules.py`, which turns
    them into "does this person want to be told about this". Nothing else may
    read them - the rule about how the switches combine is written there, once,
    next to the reason.

    The call and delivery-failure switches are still saved and still change
    nothing: there are no calls in Loopd'n, and no alert is raised when a text
    or an email fails. Each field's comment says what it does TODAY.

    NOT HERE, ON PURPOSE - these already exist:

    - Muting one group is `chat.Participant.muted`.
    - The activity feed itself is `notification.Notification` (and its `read_at`).
    - Whether group messages arrive by SMS or email is
      `MessagingSettings.sms_enabled` / `email_enabled`.

    Each field's comment says what the switch is meant to do, and what changing
    it does TODAY.
    """

    class Sound(models.TextChoices):
        DEFAULT = 'default', "Loopd'n default"
        NONE = 'none', 'None'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_settings',
    )

    # --- Messages --------------------------------------------------------------

    # "Messages" - the main switch for new-message alerts.
    #   Off - no push about any message, group or direct, mention or not.
    # The three below narrow it; see notification/push/rules.py for the order they are
    # asked in.
    # Today: real, for push.
    messages = models.BooleanField(default=True, db_default=True)

    # "Group messages"
    #   On  - an alert when somebody posts in one of the person's Loops.
    #   Off - no alert; the message still arrives and counts as unread.
    # A Loop the person has muted stays silent either way
    # (`chat.Participant.muted`).
    # Today: real, for push.
    group_messages = models.BooleanField(default=True, db_default=True)

    # "Direct messages"
    #   On  - an alert when somebody messages the person one to one.
    #   Off - no alert; the message still arrives and counts as unread.
    # Today: real, for push. This is the only channel that carries a direct
    # message: a text or an email would arrive shaped like a group's
    # (chat/delivery.py:conversation_reason).
    direct_messages = models.BooleanField(default=True, db_default=True)

    # "Mentions & replies"
    #   On  - an alert when somebody mentions the person or replies to one of
    #         their messages.
    #   Off - no special treatment; an ordinary message alert may still
    #         arrive, if "Group messages" or "Direct messages" allows it.
    # ON TOP of the two above, not under them: somebody who turned group
    # messages off in a busy Loop is exactly who still wants to know when a
    # message is addressed to them. Muting the Loop silences it all the same.
    # Today: real, for push.
    mentions_replies = models.BooleanField(default=True, db_default=True)

    # "Group invitations"
    #   On  - an alert when the person is invited to or added to a Loop.
    #   Off - no alert.
    # Covers the three events about getting into a Loop (Yul, 2026-09-16):
    # being added (`MEMBER_ADDED`), a join request of yours being approved
    # (`JOIN_APPROVED`), and - for an admin - somebody asking to join
    # (`JOIN_REQUESTED`). The map is
    # notification/push/rules.py:SETTING_BY_NOTIFICATION_TYPE.
    # Today: real, for push. The Activity feed still lists all of them either
    # way; this is only the interruption.
    group_invitations = models.BooleanField(default=True, db_default=True)

    # --- Calls -----------------------------------------------------------------

    # "Incoming calls"
    #   On  - an alert (a ring) when somebody calls the person.
    #   Off - no alert for incoming calls.
    # Today: saved, but changes nothing - there are no calls in Loopd'n yet.
    incoming_calls = models.BooleanField(default=True, db_default=True)

    # "Missed calls"
    #   On  - an alert after a call the person did not answer.
    #   Off - no alert.
    # Today: saved, but changes nothing - there are no calls in Loopd'n yet.
    missed_calls = models.BooleanField(default=True, db_default=True)

    # --- External delivery -----------------------------------------------------

    # "SMS delivery failures"
    #   On  - an alert when a message the person sent could not reach somebody
    #         by text.
    #   Off - no alert. The activity feed still records it.
    # ONE alert per message, not per person: "4 people did not get your
    # message". Raised only once the channel has given up - a permanent
    # reason, the attempts spent, or the retry window closed - because the
    # retry sweep quietly fixes most failures minutes later. Written by
    # sms/maintenance.py:announce_failed_deliveries through
    # notification/delivery_alerts.py.
    # Today: real, for push.
    sms_delivery_failures = models.BooleanField(default=True, db_default=True)

    # "Email delivery failures"
    #   On  - an alert when a message the person sent could not reach somebody
    #         by email.
    #   Off - no alert. The activity feed still records it.
    # The same shape as the SMS row above, plus the provider's own verdicts: a
    # bounce and a complaint are failures no retry can change.
    # Today: real, for push.
    email_delivery_failures = models.BooleanField(default=True, db_default=True)

    # --- Notification preview --------------------------------------------------

    # "Show sender and message"
    #   On  - an alert shows who wrote and what they wrote.
    #   Off - it only says there is a new message, so nobody glancing at the
    #         screen can read it.
    # Today: real, for push - see notification/push/payloads.py, which is the only place
    # that builds the words anybody reads.
    show_preview = models.BooleanField(default=True, db_default=True)

    # "Sound" - what plays when an alert arrives: "Loopd'n default", or "None"
    # for silence. More sounds can join the choices once there are sound files.
    # Today: HALF real. A browser will not play a file of our choosing - the
    # `sound` member of the Notification API is unimplemented everywhere - so
    # "default" is whatever sound the operating system makes, and "None" is
    # honoured as a silent notification.
    sound = models.CharField(
        max_length=8,
        choices=Sound.choices,
        default=Sound.DEFAULT,
        db_default=Sound.DEFAULT,
    )
