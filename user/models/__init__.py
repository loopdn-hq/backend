"""The user app's models, one file per model.

Everything is re-exported here, so `from user.models import Profile` works
exactly as it did when this was a single models.py - including migration
0001, which refers to `user.models.avatar_upload_to` by path.

The five `*Settings` models are one per settings screen, one row per user,
created when the user signs in (user/user_settings.py) or on first use
(`Model.for_user(user)`, see base.py). Before adding a
field to one, check it is not already somewhere else: each model's docstring
lists what deliberately lives elsewhere.
"""
from .appearance_settings import AppearanceSettings
from .messaging_settings import MessagingSettings
from .notification_settings import NotificationSettings
from .privacy_settings import PrivacySettings
from .profile import Profile, avatar_upload_to, phone_number_validator
from .security_settings import SecuritySettings
from .user_block import UserBlock

__all__ = [
    'AppearanceSettings',
    'MessagingSettings',
    'NotificationSettings',
    'PrivacySettings',
    'Profile',
    'SecuritySettings',
    'UserBlock',
    'avatar_upload_to',
    'phone_number_validator',
]
