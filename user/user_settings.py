"""Every settings row a signed-in account has.

Rows are made when the user SIGNS IN - password login, Google and verify-email
all call `ensure_settings` just before they hand out tokens. Not at
registration: an account that never verifies never signs in, so it never gets
rows, and an account from before these models existed gets its rows the next
time it signs in, with no data migration.

Nothing depends on sign-in having happened. Code that reaches a user some other
way calls `Model.for_user` (models/base.py), which makes a missing row itself.
MessagingSettings is also made at registration by user/signals.py, because the
delivery code reads it for accounts that may never have signed in; listing it
here as well costs one read and keeps this the one list of settings models.
"""
from user.models import (
    AppearanceSettings,
    MessagingSettings,
    NotificationSettings,
    PrivacySettings,
    SecuritySettings,
)

# One per settings screen. A new screen's model is added here and nowhere else.
SETTINGS_MODELS = (
    AppearanceSettings,
    MessagingSettings,
    NotificationSettings,
    PrivacySettings,
    SecuritySettings,
)


def ensure_settings(user):
    """Create whichever of the user's settings rows are missing.

    One read per model, and an insert only for a row that is not there, so
    signing in again writes nothing and never resets a choice.
    """
    for model in SETTINGS_MODELS:
        model.for_user(user)
