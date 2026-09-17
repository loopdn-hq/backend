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
    it, not a detail. The API already reads and saves it
    (GET/PATCH /users/me/settings/appearance/), but the web screen never calls
    that.

    The values match the web app's exactly (frontend lib/app/appearanceOptions.ts
    and lib/theme/ThemeProvider.tsx), so either side can be the source.

    Each field's comment says what the choice does in the app, and what
    changing this row does TODAY.
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

    # Theme - the app's colours.
    #   Light           - light backgrounds with dark text.
    #   Dark            - dark backgrounds with light text; easier on the eyes
    #                     at night.
    #   Match my device - whichever of the two the phone or computer itself is
    #                     set to.
    # On the web, picking one recolours the whole app straight away.
    # Today: changing this row changes nothing - the web uses its own copy in
    # the browser (`loopdn_theme`).
    theme = models.CharField(
        max_length=6,
        choices=Theme.choices,
        default=Theme.SYSTEM,
        db_default=Theme.SYSTEM,
    )

    # Text size - how big text is across the app: Small, Default, Large or
    # Extra large. Bigger is easier to read; smaller fits more on the screen.
    # On the web, picking one resizes the text on every page straight away.
    # Today: changing this row changes nothing - the web uses its own copy in
    # the browser (`loopdn_appearance`).
    text_size = models.CharField(
        max_length=7,
        choices=TextSize.choices,
        default=TextSize.DEFAULT,
        db_default=TextSize.DEFAULT,
    )

    # Message spacing - how tightly a conversation is packed.
    #   Comfortable - the normal gaps between messages.
    #   Compact     - messages sit closer together and their bubbles are
    #                 slimmer, so more of a conversation fits on the screen.
    # On the web, picking one re-spaces every conversation straight away (the
    # `compact:` classes in components/home/message/MessageBubble.tsx).
    # Today: changing this row changes nothing - the web uses its own copy in
    # the browser (`loopdn_appearance`).
    message_spacing = models.CharField(
        max_length=11,
        choices=MessageSpacing.choices,
        default=MessageSpacing.COMFORTABLE,
        db_default=MessageSpacing.COMFORTABLE,
    )
