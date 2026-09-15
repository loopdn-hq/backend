"""Settings → Messaging."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class MessagingSettings(UserSettings):
    """How a person sends messages, and how messages reach them outside the app.

    Account-wide. Anything that applies to one group or conversation belongs on
    `chat.Participant`, `chat.ChannelConsent` or `chat.Conversation` instead.

    TWO KINDS OF FIELD LIVE HERE, AND THEY ARE WRITTEN DIFFERENTLY:

    - The delivery switches - `sms_enabled`, `email_enabled` and the two SMS
      timestamps - are consent. A carrier STOP or START, a bounce and a
      complaint write them, and the delivery code reads them before every text
      and email. They moved here from Profile in user/0009; the old Profile
      columns stay in the database until a later release drops them (see that
      migration for what the follow-up must do first).
    - Everything else is a preference that only the person changes.

    EVERY ACCOUNT HAS A ROW. The post_save signal in user/signals.py creates it
    with the Profile, and 0009 made one for every account that already existed.
    Read it in a hot path with `of(user)`, which never writes; change it with
    `for_user(user)`, which creates the row if it is somehow missing, so an
    opt-out can never be lost.

    Each field's comment names its control on Settings → Messaging, what
    changing it does, and what it does TODAY. The web saves every row of that
    screen except the SMS box to the account (PATCH /users/me/settings/messaging/),
    but only `sms_enabled`, `email_enabled`, `enter_to_send` and
    `link_previews_enabled` change anything yet.
    """

    class ExternalContactMethod(models.TextChoices):
        ASK = 'ask', 'Ask me every time'
        SMS = 'sms', 'SMS'
        EMAIL = 'email', 'Email'

    # The web spells these `always` / `wifi` / `never`; frontend
    # lib/user/accountSettings.ts translates both ways.
    class MediaAutoDownload(models.TextChoices):
        WIFI_MOBILE = 'wifi_mobile', 'Wi-Fi and mobile data'
        WIFI_ONLY = 'wifi_only', 'Wi-Fi only'
        NEVER = 'never', 'Never'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='messaging_settings',
    )

    # --- Delivery switches (consent) -----------------------------------------

    # "Allow messages to be delivered by SMS" - Settings → Messaging → SMS.
    # The master switch for every text this account can receive, from every
    # Loop.
    #   On  - each Loop decides for itself whether it texts the person: its own
    #         `chat.Participant.sms_enabled`, plus a verified phone number.
    #   Off - no Loop texts them. Group messages and notices such as
    #         invitations are skipped as "sms disabled account-wide"
    #         (sms/outbound.py); the messages still show in the app.
    # Turning it on never opts anybody into anything - it only stops blocking:
    #
    #     send = participant.sms_enabled and messaging_settings.sms_enabled
    #
    # WHO CHANGES IT: only the person's phone. Replying STOP to any Loop's
    # number turns it off; replying START to that same number turns it back on
    # (sms/inbound.py). Nothing in the app can: the API and Django admin show
    # it read-only, and the web checkbox only keeps a copy in that browser. An
    # app switch that could turn texts back on would undo somebody's STOP.
    #
    # Members with no account have no row, so replying STOP is their only way
    # out - which is why STOP handling is not optional. Phone verification
    # codes are not affected by it.
    sms_enabled = models.BooleanField(default=True, db_default=True)

    # Not a control - a record. When a STOP last turned `sms_enabled` off;
    # empty if the person has never replied STOP. Kept apart from the flag
    # because "never opted out" and "opted out, then back in" are different
    # facts.
    sms_opt_out_at = models.DateTimeField(null=True, blank=True)

    # Not a control - a record. When a START last turned `sms_enabled` back on.
    sms_consent_at = models.DateTimeField(null=True, blank=True)

    # "Allow messages to be delivered by email" - Settings → Messaging → Email.
    # The master switch for group email, from every Loop.
    #   On  - each Loop decides for itself whether it emails the person: its
    #         `chat.ChannelConsent` row, which the link at the bottom of that
    #         Loop's emails turns off.
    #   Off - no Loop emails them; group messages are skipped as "email
    #         disabled account-wide" (mail/outbound.py). They still show in the
    #         app.
    # Turning it back on does not restart a Loop stopped with its footer link -
    # there is no per-Loop switch in the app yet.
    #
    # WHO CHANGES IT: the person, with that checkbox (PATCH
    # /users/me/settings/messaging/; PATCH /users/me/profile/ still carries it
    # under this name). It also turns itself OFF when a Loop email to them
    # bounces permanently or is reported as spam (mail/receipts.py).
    #
    # Only group email. Verification codes and password resets still arrive -
    # an account that cannot receive the code to get back into itself is an
    # account somebody has locked themselves out of, and nobody who unticks
    # "email updates" means that.
    email_enabled = models.BooleanField(default=True, db_default=True)

    # --- Preferences -----------------------------------------------------------

    # "Press Enter to send" - Settings → Messaging → Sending messages.
    #   On  - pressing Enter in the chat box sends the message.
    #   Off - Enter does nothing (the web chat box is a single line); Ctrl+Enter,
    #         or Cmd+Enter on a Mac, sends. For people who hit Enter before they
    #         have finished.
    # Today: real on the web. The saved value is loaded into the browser when
    # the app opens, and the chat box (components/home/HomeShell.tsx) follows it.
    enter_to_send = models.BooleanField(default=True, db_default=True)

    # "Show link previews" - Settings → Messaging → Sending messages.
    #   On  - a link in a message shows a small card with the page's title,
    #         description or image.
    #   Off - just the link.
    # Today: real. It is the SENDER's choice: with it on, the first link in a
    # message they send gets a card, fetched by the server after sending and
    # shown to everyone in the chat (chat/link_previews.py). Off, their links
    # go out as plain text. Changing it does not touch cards already sent.
    link_previews_enabled = models.BooleanField(default=True, db_default=True)

    # "Default way to contact people without Loopd'n" - Settings → Messaging →
    # External messaging. How to reach somebody who has no account:
    #   Ask me every time - pick SMS or email each time.
    #   SMS               - text them.
    #   Email             - email them.
    # Today: saved, but changes nothing - nothing in the app asks this yet.
    default_external_contact_method = models.CharField(
        max_length=12,
        choices=ExternalContactMethod.choices,
        default=ExternalContactMethod.ASK,
        db_default=ExternalContactMethod.ASK,
    )

    # "Send SMS when recipient is available on Loopd'n" - Settings → Messaging
    # → SMS. Whether people who DO have the app also get messages by text:
    #   On  - yes, a text as well as the message in the app.
    #   Off - the app only. People without an account are texted either way.
    # Today: saved, but changes nothing. Delivery already behaves as if it were
    # On - every member with a verified number, in a Loop whose texts are on, is
    # texted whether or not they use the app (sms/outbound.py) - so Off is the
    # behaviour still to build. `sms_enabled` above and a STOP always win.
    sms_to_loopd_users_enabled = models.BooleanField(default=True, db_default=True)

    # "Photo & video auto-download" - Settings → Messaging → Media.
    #   Wi-Fi and mobile data - photos and videos in chats download by
    #                           themselves on any connection.
    #   Wi-Fi only            - by themselves on Wi-Fi only; on mobile data
    #                           they wait for a tap, which saves data.
    #   Never                 - always wait for a tap.
    # Today: saved, but changes nothing - there is no auto-download in the app
    # for it to control yet.
    media_auto_download = models.CharField(
        max_length=12,
        choices=MediaAutoDownload.choices,
        default=MediaAutoDownload.WIFI_ONLY,
        db_default=MediaAutoDownload.WIFI_ONLY,
    )

    @classmethod
    def of(cls, user):
        """The user's row for READING, without ever writing one.

        For the delivery code, which asks this about every member of a group: a
        fan-out must not insert a row per member. Every account has a row (see
        the class docstring); if one is somehow missing the answer is the
        defaults - and the defaults are right, because every opt-out creates
        the row it writes.
        """
        try:
            return user.messaging_settings
        except cls.DoesNotExist:
            return cls(user=user)
