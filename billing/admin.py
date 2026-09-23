from django.contrib import admin

from billing.models import (
    CreditPackage,
    Plan,
    StripeWebhookEvent,
    Subscription,
    Wallet,
    WalletTransaction,
)


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'event_id', 'received_at')
    list_filter = ('event_type',)
    search_fields = ('event_id',)
    ordering = ('-received_at',)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'code', 'monthly_price_cents', 'max_groups',
        'max_members_per_group', 'max_call_participants', 'has_call_recording',
        'is_custom_pricing', 'active', 'sort_order',
    )
    list_filter = ('active', 'is_custom_pricing')
    search_fields = ('name', 'code')
    ordering = ('sort_order', 'id')


@admin.register(CreditPackage)
class CreditPackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'price_cents', 'credit_cents', 'active', 'sort_order')
    list_filter = ('active',)
    search_fields = ('name',)
    ordering = ('sort_order', 'id')


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'plan', 'status', 'current_period_end', 'cancel_at_period_end',
    )
    list_filter = ('status', 'plan')
    search_fields = ('user__username', 'stripe_customer_id', 'stripe_subscription_id')
    raw_id_fields = ('user',)


class WalletTransactionInline(admin.TabularInline):
    model = WalletTransaction
    extra = 0
    fields = ('kind', 'amount_cents', 'balance_after_cents', 'description', 'created_at')
    readonly_fields = fields
    can_delete = False
    ordering = ('-created_at',)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'balance_cents', 'auto_recharge_enabled', 'auto_recharge_package')
    search_fields = ('user__username',)
    raw_id_fields = ('user',)
    inlines = [WalletTransactionInline]
