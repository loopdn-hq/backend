"""Profile editing folded into the existing User admin.

Registering Profile as its own page would mean two clicks and two forms to
change one person, so it is an inline on User instead.
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.utils import timezone

from user.models import MessagingSettings, Profile


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    verbose_name_plural = 'Profile'
    readonly_fields = ['created_at', 'updated_at', 'last_seen_at']


class MessagingSettingsInline(admin.StackedInline):
    """The account-wide SMS and email switches, beside the profile they left.

    They moved off Profile in user/0009. Staff may switch texts and group email
    either way; the records of when each was last turned off (and, for email,
    why) are read-only, written for staff by `UserAdmin.save_formset`. A STOP is
    per group and lives on that membership's SMS consent, not here.
    """

    model = MessagingSettings
    can_delete = False
    verbose_name_plural = 'Messaging settings'
    readonly_fields = [
        'sms_opt_out_at', 'email_opt_out_at', 'email_opt_out_reason',
        'created_at', 'updated_at',
    ]


class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline, MessagingSettingsInline]
    list_display = ['username', 'email', 'get_display_name', 'is_staff', 'date_joined']
    list_select_related = ['profile']

    def save_formset(self, request, form, formset, change):
        """Record staff turning somebody's texts or group email off, as for anybody.

        By the time this runs the form has already set the switch on the row,
        so only the record is missing - when, and for email that it was staff.
        """
        if formset.model is MessagingSettings:
            for inline_form in formset.forms:
                switches = inline_form.instance
                if 'sms_enabled' in inline_form.changed_data and not switches.sms_enabled:
                    switches.sms_opt_out_at = timezone.now()
                if 'email_enabled' in inline_form.changed_data and not switches.email_enabled:
                    off = MessagingSettings.email_off(
                        MessagingSettings.EmailOptOutReason.STAFF
                    )
                    for field, value in off.items():
                        setattr(switches, field, value)
        super().save_formset(request, form, formset, change)

    @admin.display(description='Display name', ordering='profile__display_name')
    def get_display_name(self, obj):
        # A user created before the signal existed would have no profile.
        profile = getattr(obj, 'profile', None)
        return profile.display_label if profile else '-'


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
