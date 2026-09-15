"""Settings → Privacy."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class PrivacySettings(UserSettings):
    """Who can reach a person, who can find them, and what others can see.

    NOTHING READS THESE YET. Until the backend does, choosing "Nobody" does not
    stop a direct message, and switching discoverability off hides nobody. The
    web's Privacy screen does save them to the account
    (PATCH /users/me/settings/privacy/), so a choice is kept for when they are
    enforced.

    NOT HERE, ON PURPOSE - these already exist:

    - Blocking somebody is `UserBlock` (user_block.py), and it IS enforced.
    - Presence itself is `Profile.is_online` / `last_seen_at`, and how far each
      member has read is `chat.Participant.last_read_message`. The fields below
      only decide whether OTHER people are shown them.

    Each field's comment says what the setting is meant to do, and what
    changing it does TODAY.
    """

    class Audience(models.TextChoices):
        EVERYONE = 'everyone', 'Everyone'
        # There is no contacts list in the product; sharing a Loop is the one
        # relationship that exists.
        LOOPS = 'loops', 'People in my Loops'
        NOBODY = 'nobody', 'Nobody'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='privacy_settings',
    )

    # --- Messaging & contact ---------------------------------------------------

    # "Who can send me direct messages" - who may START a one-to-one
    # conversation with the person.
    #   Everyone           - anybody who finds them.
    #   People in my Loops - only people who share a Loop with them.
    #   Nobody             - nobody.
    # Somebody blocked in either direction never can, whatever this says
    # (`UserBlock`, already enforced).
    # Today: saved, but changes nothing - anybody who finds the person can
    # still start a direct message.
    direct_messages_from = models.CharField(
        max_length=8,
        choices=Audience.choices,
        default=Audience.EVERYONE,
        db_default=Audience.EVERYONE,
    )

    # "Who can add me to groups" - who may put the person into a Loop.
    #   Everyone           - anybody adding members to a Loop.
    #   People in my Loops - only people who already share a Loop with them.
    #   Nobody             - nobody.
    # Today: saved, but changes nothing - anybody who can add members to a Loop
    # can still add the person.
    group_adds_from = models.CharField(
        max_length=8,
        choices=Audience.choices,
        default=Audience.EVERYONE,
        db_default=Audience.EVERYONE,
    )

    # --- Discoverability -------------------------------------------------------
    #
    # Enforcing these means gating `user/lookups.py:find_user` - which resolves
    # an exact verified number or email when somebody starts a direct message
    # or adds members - and not only GET /users/search/.

    # "Find me using my phone number"
    #   On  - somebody who types the person's exact phone number - searching
    #         for people, starting a direct message, adding members - finds
    #         their account.
    #   Off - typing the number does not reveal the account.
    # Today: saved, but changes nothing - a verified number still finds them.
    discoverable_by_phone = models.BooleanField(default=True, db_default=True)

    # "Find me using my email"
    #   On  - somebody who types the person's exact email address - starting a
    #         direct message, adding members, inviting by email - finds their
    #         account.
    #   Off - typing the address does not reveal the account.
    # Today: saved, but changes nothing - a verified address still finds them.
    discoverable_by_email = models.BooleanField(default=True, db_default=True)

    # --- Activity --------------------------------------------------------------

    # "Read receipts"
    #   On  - other members can see when the person has read their messages
    #         (who has read it, in a message's info).
    #   Off - other members are not shown that.
    # Today: saved, but changes nothing - a message's info still shows who has
    # read it (`chat.Participant.last_read_message`).
    read_receipts_enabled = models.BooleanField(default=True, db_default=True)

    # "Online status"
    #   On  - other members see a green dot while the person is online, and
    #         when they were last seen once they are not.
    #   Off - other members are shown neither.
    # Today: saved, but changes nothing - a conversation's member list still
    # shows the dot, and "last seen" on hover (`Profile.is_online` /
    # `last_seen_at`).
    online_status_visible = models.BooleanField(default=True, db_default=True)
