"""Staff views of WhatsApp. The token is never shown, and SUSPENDED is set here."""
from django.contrib import admin

from whatsapp.models import (
    GroupChannel,
    WhatsAppConnection,
    WhatsAppContact,
    WhatsAppDelivery,
)


class GroupChannelInline(admin.TabularInline):
    model = GroupChannel
    fk_name = 'whatsapp_connection'
    extra = 0
    fields = ['conversation', 'keyword', 'delivery_enabled', 'replies_enabled']
    raw_id_fields = ['conversation']


@admin.register(WhatsAppConnection)
class WhatsAppConnectionAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'owner', 'display_phone_number', 'status',
                    'quality_rating', 'connected_at']
    list_filter = ['status']
    search_fields = ['display_phone_number', 'verified_name', 'waba_id',
                     'phone_number_id', 'owner__username']
    raw_id_fields = ['owner']
    # The encrypted token is excluded outright, not merely read-only.
    exclude = ['access_token_encrypted']
    readonly_fields = ['waba_id', 'phone_number_id', 'business_id', 'display_phone_number',
                       'verified_name', 'quality_rating', 'messaging_limit_tier',
                       'token_expires_at', 'last_error', 'connected_at',
                       'disconnected_at', 'updated_at']
    inlines = [GroupChannelInline]


@admin.register(WhatsAppContact)
class WhatsAppContactAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'connection', 'last_inbound_at', 'current_conversation']
    raw_id_fields = ['connection', 'current_conversation']


@admin.register(WhatsAppDelivery)
class WhatsAppDeliveryAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'direction', 'kind', 'status', 'reason',
                    'conversation', 'counterpart_last4']
    list_filter = ['direction', 'status', 'kind']
    raw_id_fields = ['connection', 'conversation', 'message', 'participant']
    readonly_fields = [f.name for f in WhatsAppDelivery._meta.fields]
