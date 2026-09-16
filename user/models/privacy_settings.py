"""Settings → Privacy."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class PrivacySettings(UserSettings):
    """Who can reach a person, who can find them, and what others can see.

    ENFORCED, except "Online status" - see its field. `user/privacy.py` is the
    one place that reads them, the way `user/blocks.py` is the one place that
    reads a block, and every refusal is asked there rather than copied into
    each caller. The web's Privacy screen saves them with
    PATCH /users/me/settings/privacy/.

    A BLOCK IS ASKED FIRST AND ALWAYS WINS. It is a fact about a pair; these
    are one person's standing answer to everybody, and their refusals say
    plainly what they are - a block's wording deliberately reveals nothing.

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
    # Today: real. `chat/serializers.py:DirectConversationSerializer` refuses
    # with "This person doesn't accept direct messages." Only STARTING one is
    # refused: a thread the two already have carries on, because stranding a
    # conversation somebody is in is what blocking is for.
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
    # Today: real, on every route that adds somebody else: creating a group
    # with members, the add-members endpoint, an email invitation to a verified
    # account, and the moment a phone or email invitation is claimed - which is
    # the first moment the row has an account to ask. The add is all-or-nothing
    # and the refusal names who refused, so an admin can take them off the list.
    # Joining by link, code or a join request is their own doing and is never
    # refused by this.
    group_adds_from = models.CharField(
        max_length=8,
        choices=Audience.choices,
        default=Audience.EVERYONE,
        db_default=Audience.EVERYONE,
    )

    # --- Discoverability -------------------------------------------------------
    #
    # Both are read by `user/lookups.py` - `find_user`, which resolves an exact
    # verified number or address when somebody starts a direct message, adds
    # members or invites by email, and `search_users` behind GET
    # /users/search/. Neither hides a @username: these are about the
    # identifiers people hand out.

    # "Find me using my phone number"
    #   On  - somebody who types the person's exact phone number - searching
    #         for people, starting a direct message, adding members - finds
    #         their account.
    #   Off - typing the number does not reveal the account.
    # Today: real, in `find_user` and in search.
    discoverable_by_phone = models.BooleanField(default=True, db_default=True)

    # "Find me using my email"
    #   On  - somebody who types the person's exact email address - starting a
    #         direct message, adding members, inviting by email - finds their
    #         account.
    #   Off - typing the address does not reveal the account. An invitation to
    #         that address is not even emailed: it could never be claimed, and
    #         `invite_by_email` returns None rather than say why.
    # Today: real, in `find_user`.
    discoverable_by_email = models.BooleanField(default=True, db_default=True)

    # --- Activity --------------------------------------------------------------

    # "Read receipts"
    #   On  - other members can see when the person has read their messages
    #         (who has read it, in a message's info).
    #   Off - other members are not shown that, AND they are not shown anybody
    #         else's either. The bargain is two-way (Yul's call, 2026-09-16):
    #         one-way would take receipts without giving any.
    # Their own read position is always their own to see - it clears their own
    # badge rather than telling anybody anything.
    # Today: real, in both places it could leak: the member list
    # (`ParticipantSerializer`) and the live `conversation.read.by` frame,
    # which `chat/services.py` does not send and `chat/consumers.py` does not
    # forward.
    read_receipts_enabled = models.BooleanField(default=True, db_default=True)

    # "Online status"
    #   On  - other members see a green dot while the person is online, and
    #         when they were last seen once they are not.
    #   Off - other members are shown neither.
    # Today: THE ONLY ONE NOT ENFORCED, because there is nothing to hide yet:
    # nothing ever sets `Profile.is_online` or `last_seen_at`, so everybody
    # reads as offline. When presence is built, this is one check in
    # `PublicProfileSerializer` plus whatever broadcasts it.
    online_status_visible = models.BooleanField(default=True, db_default=True)
