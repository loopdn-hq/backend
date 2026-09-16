"""Staff-only view of who has notifications on, for answering "why no buzz".

Read-only throughout. A subscription is written by a browser and deleted by the
push service refusing it; a person editing one by hand can only produce a row
that encrypts to nothing.
"""
from django.contrib import admin

from notification.models import PushSubscription


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'short_endpoint', 'user_agent', 'created_at',
                    'last_used_at')
    list_select_related = ('user',)
    search_fields = ('user__username', 'user__email', 'endpoint')
    readonly_fields = ('user', 'endpoint', 'p256dh', 'auth', 'user_agent',
                       'created_at', 'last_used_at')

    def has_add_permission(self, request):
        return False

    @admin.display(description='Endpoint')
    def short_endpoint(self, obj):
        """The provider and a stub. The full URL is the address of somebody's
        device and is not something to have on a list page."""
        return f'{obj.endpoint[:48]}...'
