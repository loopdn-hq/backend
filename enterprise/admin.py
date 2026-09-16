from django.contrib import admin

from enterprise.models import (
    BillingIntervalOption,
    CreditLedgerEntry,
    EnterpriseBaseFee,
    EnterpriseContract,
    EnterpriseEntitlement,
    EnterprisePlanDraft,
    EnterpriseStripeBilling,
    PlanFeature,
    PricingCatalogVersion,
    PricingOption,
)


class PricingOptionInline(admin.TabularInline):
    model = PricingOption
    extra = 0


class PlanFeatureInline(admin.TabularInline):
    model = PlanFeature
    extra = 0


class BillingIntervalOptionInline(admin.TabularInline):
    model = BillingIntervalOption
    extra = 0


class EnterpriseBaseFeeInline(admin.StackedInline):
    model = EnterpriseBaseFee
    extra = 0


@admin.register(PricingCatalogVersion)
class PricingCatalogVersionAdmin(admin.ModelAdmin):
    list_display = ('version', 'is_active', 'created_at')
    inlines = [EnterpriseBaseFeeInline, PricingOptionInline, PlanFeatureInline, BillingIntervalOptionInline]


@admin.register(EnterprisePlanDraft)
class EnterprisePlanDraftAdmin(admin.ModelAdmin):
    list_display = ('draft_id', 'account', 'status', 'calculated_total_cents', 'created_at', 'expires_at')
    search_fields = ('draft_id', 'account__email')
    list_filter = ('status',)


@admin.register(EnterpriseContract)
class EnterpriseContractAdmin(admin.ModelAdmin):
    list_display = ('contract_number', 'account', 'billing_interval', 'status', 'created_at')
    search_fields = ('contract_number', 'account__email')
    list_filter = ('status', 'billing_interval')


@admin.register(EnterpriseStripeBilling)
class EnterpriseStripeBillingAdmin(admin.ModelAdmin):
    list_display = ('contract', 'stripe_customer_id', 'stripe_subscription_id', 'status')


@admin.register(EnterpriseEntitlement)
class EnterpriseEntitlementAdmin(admin.ModelAdmin):
    list_display = ('contract', 'feature_key', 'enabled', 'limit_value', 'limit_type')
    list_filter = ('feature_key', 'enabled')


@admin.register(CreditLedgerEntry)
class CreditLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ('contract', 'transaction_type', 'amount', 'balance_after', 'created_at')
    list_filter = ('transaction_type',)
