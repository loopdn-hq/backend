"""Admin for the SMS side: numbers, the delivery log, and the kill switch.

Deliberately lopsided. Numbers and settings are editable because somebody has to
operate them; the delivery log and the webhook ledger are strictly read-only,
because they are a record of what happened and an editable record is not one.

The same restraint as chat/admin.py: this shows *that* a message was sent, to a
number ending in four digits, and what it cost - never the message body, and
never a subscriber's full phone number. An admin screen is the easiest place in
a product to accidentally build a window onto everybody's private conversations.
"""
from django.contrib import admin

from sms.models import (
    GroupNumber,
    NumberLease,
    NumberRentalMonth,
    SmsDelivery,
    SmsSettings,
    SmsUsageMonth,
    SmsWebhookEvent,
    VerificationText,
)


@admin.register(GroupNumber)
class GroupNumberAdmin(admin.ModelAdmin):
    """The routing table: which number reaches which group."""

    list_display = ['number', 'status', 'conversation', 'country', 'provider']
    list_filter = ['status', 'country', 'provider']
    search_fields = ['number', 'conversation__name']
    # Every field. A group's number is fixed for the life of the group - people
    # save it in their phones as "Family" - so reassigning one here would
    # silently deliver a stranger's messages into somebody's conversation.
    # Visible for support, editable by nobody.
    readonly_fields = [
        'number', 'conversation', 'status', 'country', 'provider',
        'provider_number_id', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        # Numbers arrive by being provisioned for a group, never by being typed
        # into a form - a hand-made row would be a number nobody leased.
        return False

    def has_delete_permission(self, request, obj=None):
        # Deleting the group is what deletes the number, so that the provider
        # is told and the lease actually stops.
        return False


@admin.register(SmsDelivery)
class SmsDeliveryAdmin(admin.ModelAdmin):
    """Every message in or out, and what it cost. Read-only.

    This is the answer to the two questions support actually gets: "why did I
    not get that message" - look for a SKIPPED row and read its reason - and
    "what am I being charged for" - the cost, or the segments where the
    provider has not priced it yet. A failure carries the provider's own code.
    """

    list_display = [
        'created_at',
        'direction',
        'conversation',
        'recipient',
        'status',
        'segments',
        'cost',
        'reason',
        'error_code',
        'error_detail',
    ]
    list_filter = ['direction', 'status', 'error_code', 'encoding', 'created_at']
    # Not by number: the column holds only a hash and the last four digits, so
    # there is nothing here to search a subscriber by, on purpose.
    search_fields = ['conversation__name', 'provider_message_id']
    date_hierarchy = 'created_at'
    list_select_related = ['conversation', 'user']

    @admin.display(description='to/from')
    def recipient(self, obj):
        return f'…{obj.counterpart_last4}' if obj.counterpart_last4 else '—'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SmsWebhookEvent)
class SmsWebhookEventAdmin(admin.ModelAdmin):
    """Which provider events have been handled. Read-only.

    Worth looking at exactly once: when a provider insists it delivered
    something and the product disagrees.
    """

    list_display = ['received_at', 'event_type', 'provider_event_id']
    list_filter = ['event_type', 'received_at']
    search_fields = ['provider_event_id']
    date_hierarchy = 'received_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SmsSettings)
class SmsSettingsAdmin(admin.ModelAdmin):
    """The kill switch. One row, and it must stay one row."""

    list_display = ['__str__', 'note', 'updated_at']
    readonly_fields = ['updated_at']

    def has_add_permission(self, request):
        # There is exactly one row, created by the first save or by a migration.
        # A second would mean two answers to "is sending on".
        return not SmsSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SmsUsageMonth)
class SmsUsageMonthAdmin(admin.ModelAdmin):
    """Monthly totals per group and account - the billing summary.

    Kept after the delivery log behind it has been purged. Read-only: it is
    rebuilt from that log every day, so an edit here would be overwritten.
    """

    list_display = [
        'month',
        'conversation_name',
        'user',
        'direction',
        'texts',
        'segments',
        'cost',
        'cost_currency',
        'priced',
        'delivered',
        'failed',
        'skipped',
    ]
    list_filter = ['month', 'direction']
    search_fields = ['conversation_name', 'user__username']
    list_select_related = ['user']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(NumberLease)
class NumberLeaseAdmin(admin.ModelAdmin):
    """Every number we rented, its price, and how long we held it.

    Kept after the number is released - the GroupNumber row is not. A lease
    with no release date is a number still being billed.
    """

    list_display = [
        'number',
        'conversation_name',
        'country',
        'monthly_cost',
        'upfront_cost',
        'cost_currency',
        'leased_at',
        'released_at',
    ]
    list_filter = ['country', 'provider', 'released_at']
    search_fields = ['number', 'conversation_name']
    date_hierarchy = 'leased_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NumberRentalMonth)
class NumberRentalMonthAdmin(admin.ModelAdmin):
    """Number rental, month by month - the rental lines of a monthly bill.

    Read-only, rebuilt every day from the leases.
    """

    list_display = [
        'month',
        'number',
        'conversation_name',
        'monthly_cost',
        'upfront_cost',
        'cost_currency',
    ]
    list_filter = ['month']
    search_fields = ['number', 'conversation_name']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(VerificationText)
class VerificationTextAdmin(admin.ModelAdmin):
    """Every sign-up and login code text: asked for, stopped, sent, arrived.

    For what they cost, which fail, and abuse. Never the code itself - this
    system never has it. Read-only.
    """

    list_display = [
        'created_at',
        'user',
        'country',
        'recipient',
        'outcome',
        'reason',
        'delivery_status',
        'cost',
        'error_code',
        'error_detail',
    ]
    list_filter = ['outcome', 'delivery_status', 'country', 'error_code', 'created_at']
    search_fields = ['user__username']
    date_hierarchy = 'created_at'
    list_select_related = ['user']

    @admin.display(description='to')
    def recipient(self, obj):
        return f'…{obj.counterpart_last4}' if obj.counterpart_last4 else '—'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
