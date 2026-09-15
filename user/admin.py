"""Profile editing folded into the existing User admin.

Registering Profile as its own page would mean two clicks and two forms to
change one person, so it is an inline on User instead.
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from user.models import MessagingSettings, Profile


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    verbose_name_plural = 'Profile'
    readonly_fields = ['created_at', 'updated_at', 'last_seen_at']


class MessagingSettingsInline(admin.StackedInline):
    """The account-wide SMS and email switches, beside the profile they left.

    They moved off Profile in user/0009, and staff still need to see whether
    somebody texted STOP. The SMS consent fields are read-only here: a carrier
    STOP or START is the only thing that may change them, and an admin turning
    SMS back on would text somebody who said stop.
    """

    model = MessagingSettings
    can_delete = False
    verbose_name_plural = 'Messaging settings'
    readonly_fields = [
        'sms_enabled', 'sms_opt_out_at', 'sms_consent_at', 'created_at', 'updated_at',
    ]


class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline, MessagingSettingsInline]
    list_display = ['username', 'email', 'get_display_name', 'is_staff', 'date_joined']
    list_select_related = ['profile']

    @admin.display(description='Display name', ordering='profile__display_name')
    def get_display_name(self, obj):
        # A user created before the signal existed would have no profile.
        profile = getattr(obj, 'profile', None)
        return profile.display_label if profile else '-'


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
