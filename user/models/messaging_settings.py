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
    """

    class ExternalContactMethod(models.TextChoices):
        ASK = 'ask', 'Ask me every time'
        SMS = 'sms', 'SMS'
        EMAIL = 'email', 'Email'

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

    # The master switch for every text this account could receive, across every
    # group. It only ever SUBTRACTS: turning it on does not opt anybody into
    # anything, because whether a given group texts you is decided per
    # membership on `chat.Participant.sms_enabled`. The effective rule is
    #
    #     send = participant.sms_enabled and messaging_settings.sms_enabled
    #
    # so this is the one control that stops everything at once. It defaults to
    # True and only ever subtracts.
    #
    # NO endpoint writes it, deliberately: the only thing that writes it is a
    # carrier STOP or START (sms/inbound.py). GET /users/me/settings/messaging/
    # shows it read-only. Its email counterpart below IS writable, because email
    # has no carrier word and a member needs a switch somewhere.
    # Settings → Messaging → "Allow messages to be delivered by SMS".
    #
    # Members who have no account have no row and therefore no switch here:
    # replying STOP is their only way out, which is why STOP handling is not
    # optional.
    sms_enabled = models.BooleanField(default=True, db_default=True)
    # When they last turned it off, or replied STOP. Kept apart from the flag
    # because "has never opted out" and "opted out and back in" are different
    # facts, and a complaint turns on the second one.
    sms_opt_out_at = models.DateTimeField(null=True, blank=True)
    sms_consent_at = models.DateTimeField(null=True, blank=True)

    # The same master switch as `sms_enabled`, for group email, and it works
    # the same way: it only ever subtracts. Whether a given group emails you is
    # decided per membership by `chat.ChannelConsent`; this is the one control
    # that stops every group at once. Written by the person (PATCH
    # /users/me/settings/messaging/, or PATCH /users/me/profile/, which still
    # carries it under this name) and by a complaint or a permanent bounce
    # (mail/receipts.py). Settings → Messaging
    # → "Allow messages to be delivered by email".
    #
    # Only group traffic. Verification codes and password resets are not
    # subject to it - an account that cannot receive the code to get back into
    # itself is an account somebody has locked themselves out of, and nobody
    # who unticks "email updates" means that.
    email_enabled = models.BooleanField(default=True, db_default=True)

    # --- Preferences -----------------------------------------------------------

    # Whether Enter sends. The web composer is a one-line box, so with this off
    # Enter does nothing there and Ctrl+Enter / Cmd+Enter sends.
    enter_to_send = models.BooleanField(default=True, db_default=True)

    # Whether links in conversations show a preview - the page's title,
    # description or image.
    link_previews_enabled = models.BooleanField(default=True, db_default=True)

    # How to reach somebody who has no Loopd'n account. ASK means the sender
    # picks SMS or email each time.
    default_external_contact_method = models.CharField(
        max_length=12,
        choices=ExternalContactMethod.choices,
        default=ExternalContactMethod.ASK,
        db_default=ExternalContactMethod.ASK,
    )

    # Whether an account holder ALSO gets an SMS copy of messages they can read
    # in the app. True is what the backend does today - every active member with
    # a verified number, in a group whose texts are on, is texted whether or not
    # they use the app (sms/outbound.py) - so True changes nothing and False is
    # the new behaviour to build. SMS-only members are unaffected either way,
    # and `sms_enabled` above and a carrier STOP still win.
    sms_to_loopd_users_enabled = models.BooleanField(default=True, db_default=True)

    # Which photos and videos the client downloads without being asked.
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
