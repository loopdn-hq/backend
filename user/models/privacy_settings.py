"""Settings → Privacy."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class PrivacySettings(UserSettings):
    """Who can reach a person, who can find them, and what others can see.

    NOTHING READS THESE YET. Until the backend does, choosing "Nobody" does not
    stop a direct message, and switching discoverability off hides nobody.

    NOT HERE, ON PURPOSE - these already exist:

    - Blocking somebody is `UserBlock` (user_block.py), and it IS enforced.
    - Presence itself is `Profile.is_online` / `last_seen_at`, and how far each
      member has read is `chat.Participant.last_read_message`. The fields below
      only decide whether OTHER people are shown them.
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

    # Messaging & contact
    direct_messages_from = models.CharField(
        max_length=8,
        choices=Audience.choices,
        default=Audience.EVERYONE,
        db_default=Audience.EVERYONE,
    )
    group_adds_from = models.CharField(
        max_length=8,
        choices=Audience.choices,
        default=Audience.EVERYONE,
        db_default=Audience.EVERYONE,
    )

    # Discoverability. Enforcing these means gating `user/lookups.py:find_user`
    # - which resolves an exact verified number or email when somebody starts a
    # direct message or adds members - and not only GET /users/search/.
    discoverable_by_phone = models.BooleanField(default=True, db_default=True)
    discoverable_by_email = models.BooleanField(default=True, db_default=True)

    # Activity
    read_receipts_enabled = models.BooleanField(default=True, db_default=True)
    online_status_visible = models.BooleanField(default=True, db_default=True)
