"""Settings → Appearance."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class AppearanceSettings(UserSettings):
    """How Loopd'n looks for a person.

    THE ACCOUNT COPY, AND NOTHING READS IT YET. The web app keeps these per
    DEVICE today - localStorage `loopdn_theme` and `loopdn_appearance`, applied
    before the first paint - because a phone and a laptop can reasonably want
    different sizes. This row is for when they should follow a person from
    device to device instead; that is a product decision to make before wiring
    it, not a detail.

    The values match the web app's exactly (frontend lib/app/appearanceOptions.ts
    and lib/theme/ThemeProvider.tsx), so either side can be the source.
    """

    class Theme(models.TextChoices):
        LIGHT = 'light', 'Light'
        DARK = 'dark', 'Dark'
        SYSTEM = 'system', 'Match my device'

    class TextSize(models.TextChoices):
        SMALL = 'small', 'Small'
        DEFAULT = 'default', 'Default'
        LARGE = 'large', 'Large'
        LARGER = 'larger', 'Extra large'

    class MessageSpacing(models.TextChoices):
        COMFORTABLE = 'comfortable', 'Comfortable'
        COMPACT = 'compact', 'Compact'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='appearance_settings',
    )

    theme = models.CharField(
        max_length=6,
        choices=Theme.choices,
        default=Theme.SYSTEM,
        db_default=Theme.SYSTEM,
    )
    text_size = models.CharField(
        max_length=7,
        choices=TextSize.choices,
        default=TextSize.DEFAULT,
        db_default=TextSize.DEFAULT,
    )
    message_spacing = models.CharField(
        max_length=11,
        choices=MessageSpacing.choices,
        default=MessageSpacing.COMFORTABLE,
        db_default=MessageSpacing.COMFORTABLE,
    )
