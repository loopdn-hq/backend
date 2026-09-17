from django.contrib import admin

from mail.models import EmailDelivery, EmailWebhookEvent


@admin.register(EmailDelivery)
class EmailDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'direction', 'status', 'conversation',
        'counterpart_domain', 'reason',
    )
    list_filter = ('direction', 'status', 'created_at')
    search_fields = ('counterpart_hash', 'provider_message_id', 'reason')
    # Every field is written by the delivery path. An admin editing one would
    # be rewriting the record of what happened, which is the one thing this
    # table exists to be trustworthy about.
    readonly_fields = [field.name for field in EmailDelivery._meta.fields]
    date_hierarchy = 'created_at'


@admin.register(EmailWebhookEvent)
class EmailWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('received_at', 'event_type', 'provider_event_id')
    search_fields = ('provider_event_id', 'event_type')
    readonly_fields = [field.name for field in EmailWebhookEvent._meta.fields]
