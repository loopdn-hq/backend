"""Read-only admin for pending codes.

Listed for support ("did their code go out?") and deletable so a stuck user
can be unblocked, but never editable: a staff member who could type a code
into this form could sign in as anyone.
"""
from django.contrib import admin

from otp.models import EmailOtp, SmsOtp


class BaseOtpAdmin(admin.ModelAdmin):
    list_display = ['profile', 'expiry', 'attempts', 'updated_at']
    readonly_fields = ['profile', 'code', 'expiry', 'attempts', 'timestamp', 'updated_at']
    search_fields = ['profile__user__username', 'profile__user__email']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EmailOtp)
class EmailOtpAdmin(BaseOtpAdmin):
    pass


@admin.register(SmsOtp)
class SmsOtpAdmin(BaseOtpAdmin):
    list_display = BaseOtpAdmin.list_display + ['phone_no']
    readonly_fields = BaseOtpAdmin.readonly_fields + ['phone_no']
