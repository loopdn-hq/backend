"""Settings → Messaging."""
from django.conf import settings
from django.db import models
from django.utils import timezone

from .base import UserSettings


class MessagingSettings(UserSettings):
    """How a person sends messages, and how messages reach them outside the app.

    Account-wide. Anything that applies to one group or conversation belongs on
    `chat.Participant`, `chat.ChannelConsent` or `chat.Conversation` instead -
    including a STOP, which is about ONE Loop (see `sms_enabled`).

    TWO KINDS OF FIELD LIVE HERE, AND THEY ARE WRITTEN DIFFERENTLY:

    - The delivery switches - `sms_enabled`, `email_enabled`, and the records
      of when (and, for email, why) each was last turned off. The person
      changes both, email also turns itself off on a bounce or a complaint,
      and the delivery code reads them before every text and email. They
      moved here from Profile in user/0009, and user/0012 dropped the old
      Profile columns after carrying over any opt-out still sitting in them.
    - Everything else is a preference that only the person changes.

    EVERY ACCOUNT HAS A ROW. The post_save signal in user/signals.py creates it
    with the Profile, and 0009 made one for every account that already existed.
    Read it in a hot path with `of(user)`, which never writes; change it with
    `for_user(user)`, which creates the row if it is somehow missing, so an
    opt-out can never be lost.

    Each field's comment names its control on Settings → Messaging, what
    changing it does, and what it does TODAY. The web saves every row of that
    screen to the account (PATCH /users/me/settings/messaging/). Only
    `sms_enabled`, `email_enabled`, `enter_to_send` and `link_previews_enabled`
    change anything yet.
    """

    class EmailOptOutReason(models.TextChoices):
        PERSON = 'person', 'They turned it off'
        BOUNCE = 'bounce', 'An email to them bounced'
        COMPLAINT = 'complaint', 'They marked an email as spam'
        STAFF = 'staff', 'Staff turned it off'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='messaging_settings',
    )

    # --- Delivery switches ---------------------------------------------------

    # "Send and receive Loop messages by text" - Settings → Messaging → SMS.
    # The ONE SMS switch: every Loop at once, in both directions.
    #   On  - Loops may text the person, and what they text a Loop's number is
    #         posted. Each Loop still decides for itself on top of this: its
    #         own `chat.Participant.sms_enabled`, the person's SMS consent for
    #         that Loop (`chat.ChannelConsent`), and a verified phone number.
    #   Off - the app only. No Loop texts them: group messages and notices are
    #         skipped as "sms disabled account-wide" (sms/outbound.py) and never
    #         retried. A text they send to any Loop's number is ignored
    #         silently (sms/inbound.py), except STOP, START and HELP. Messages
    #         still show in the app.
    #
    #     send = participant.sms_enabled
    #            and that Loop's SMS consent allows it
    #            and messaging_settings.sms_enabled
    #
    # WHO CHANGES IT: the person, with that checkbox (PATCH
    # /users/me/settings/messaging/), and staff in Django admin. Both go through
    # `switch_sms`, so `sms_opt_out_at` is always written.
    #
    # NOT STOP. Replying STOP to a Loop's number opts that number out of THAT
    # Loop only - its SMS consent and its membership's `sms_enabled` - so the
    # Loop neither texts it nor takes texts from it, until START to the same
    # number (sms/inbound.py). Other Loops, and this switch, are untouched.
    #
    # People with no account have no row: STOP and START, per Loop, are their
    # only controls. Phone verification codes are not affected by it.
    sms_enabled = models.BooleanField(
        'Send and receive Loop messages by text', default=True, db_default=True
    )

    # Not a control - a record. When `sms_enabled` was last turned off; kept when
    # it is turned back on, and empty if it never has been. Before user/0013 a
    # STOP to any Loop wrote this; a STOP is now recorded on that Loop's own
    # `chat.ChannelConsent.opted_out_at` instead.
    sms_opt_out_at = models.DateTimeField('SMS turned off at', null=True, blank=True)

    # "Send and receive Loop messages by email" - Settings → Messaging → Email.
    # The ONE email switch: every Loop at once, in both directions.
    #   On  - Loops may email the person, and what they reply to a Loop email is
    #         posted. Each Loop still decides for itself on top of this: the
    #         person's email consent for that Loop (`chat.ChannelConsent`).
    #   Off - the app only. No Loop emails them: group messages are skipped as
    #         "email disabled account-wide" (mail/outbound.py). A reply they send
    #         to any Loop email is ignored silently (mail/inbound.py), except
    #         STOP EMAILS. Messages still show in the app.
    #
    # NOT STOP EMAILS. Replying STOP EMAILS to a Loop's email, or pressing the
    # link at the bottom of one, opts the person out of THAT Loop only - its
    # email consent - so the Loop neither emails them nor takes their replies,
    # until they turn it back on for that Loop in the app. Other Loops, and this
    # switch, are untouched. Turning this switch back on does not undo those.
    #
    # WHO CHANGES IT: the person, with that checkbox (PATCH
    # /users/me/settings/messaging/; PATCH /users/me/profile/ still carries it
    # under this name), and staff in Django admin. It also turns itself OFF
    # when a Loop email to them bounces permanently or is reported as spam
    # (mail/receipts.py). Every one of those turns it off through `email_off` /
    # `switch_email` below, so the two records after it are always written.
    #
    # Only group email. Verification codes and password resets still arrive -
    # an account that cannot receive the code to get back into itself is an
    # account somebody has locked themselves out of, and nobody who unticks
    # "email updates" means that.
    email_enabled = models.BooleanField(
        'Send and receive Loop messages by email', default=True, db_default=True
    )

    # Not a control - a record. When `email_enabled` was last turned off;
    # empty if it never has been - or if it was turned off before this record
    # existed (user/0011), when nobody wrote down when or why. The email
    # counterpart of `sms_opt_out_at`, and kept when email is turned back on
    # for the same reason: "never opted out" and "opted out, then back in" are
    # different facts.
    email_opt_out_at = models.DateTimeField(
        'Email turned off at', null=True, blank=True
    )

    # Not a control - a record. WHY `email_enabled` was last turned off:
    #   person    - they unticked it themselves.
    #   bounce    - an email to them bounced permanently (the address is gone).
    #   complaint - they marked a Loop email as spam.
    #   staff     - somebody turned it off for them in Django admin.
    # Empty if email has never been turned off, or was turned off before
    # user/0011. What lets anybody - support, or later the screen itself - say
    # why a person's email is off.
    email_opt_out_reason = models.CharField(
        'Why email was turned off',
        max_length=10,
        choices=EmailOptOutReason.choices,
        blank=True,
        default='',
        db_default='',
    )

    # --- Preferences -----------------------------------------------------------

    # "Press Enter to send" - Settings → Messaging → Sending messages.
    #   On  - pressing Enter in the chat box sends the message.
    #   Off - Enter does nothing (the web chat box is a single line); Ctrl+Enter,
    #         or Cmd+Enter on a Mac, sends. For people who hit Enter before they
    #         have finished.
    # Today: real on the web. The saved value is loaded into the browser when
    # the app opens, and the chat box (components/home/HomeShell.tsx) follows it.
    enter_to_send = models.BooleanField(
        'Press Enter to send', default=True, db_default=True
    )

    # "Show link previews" - Settings → Messaging → Sending messages.
    #   On  - a link in a message shows a small card with the page's title,
    #         description or image.
    #   Off - just the link.
    # Today: real. It is the SENDER's choice: with it on, the first link in a
    # message they send gets a card, fetched by the server after sending and
    # shown to everyone in the chat (chat/link_previews.py). Off, their links
    # go out as plain text. Changing it does not touch cards already sent.
    link_previews_enabled = models.BooleanField(
        'Show link previews', default=True, db_default=True
    )

    def switch_sms(self, enabled):
        """Turn texts on or off for every Loop, recording when they went off.

        Returns the names of the fields it changed, for save(update_fields=...),
        and changes nothing when texts are already that way - so a repeated
        "off" never moves the record of the first one.
        """
        if self.sms_enabled == enabled:
            return []
        self.sms_enabled = enabled
        if enabled:
            return ['sms_enabled']
        self.sms_opt_out_at = timezone.now()
        return ['sms_enabled', 'sms_opt_out_at']

    @classmethod
    def email_off(cls, reason):
        """The values that turn group email off, with when and why.

        The ONE definition of turning email off. The receipts write many rows
        in a single UPDATE, which never calls save(), so this is a plain dict
        every writer can use - an UPDATE, a bulk_create, or a row in hand.
        """
        return {
            'email_enabled': False,
            'email_opt_out_at': timezone.now(),
            'email_opt_out_reason': reason,
        }

    def switch_email(self, enabled, *, reason):
        """Turn group email on or off on this row, recording why when it goes off.

        Returns the names of the fields it changed, for save(update_fields=...),
        and changes nothing when email is already that way - so a repeated
        "off" never moves the record of the first one. Turning it back on
        leaves the record in place, as `switch_sms` does.
        """
        if self.email_enabled == enabled:
            return []
        changes = {'email_enabled': True} if enabled else self.email_off(reason)
        for field, value in changes.items():
            setattr(self, field, value)
        return list(changes)

