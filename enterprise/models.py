from django.conf import settings
from django.db import models


class PricingCatalogVersion(models.Model):
    """One immutable snapshot of everything the Plan Builder can price.

    A price change is a new version with `is_active=True`, not an edit to an
    old one - `EnterpriseContract.catalog_version` and
    `EnterprisePlanDraft.catalog_version` point at a specific version so a
    later price change can never move under a contract that already froze
    its own snapshot.
    """

    version = models.PositiveIntegerField(unique=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-version']

    def __str__(self):
        return f'catalog v{self.version}'


class PricingOption(models.Model):
    """One selectable, priced choice within a category (a limit tier, an
    analytics tier, a credit package, ...) for a given catalog version."""

    class Category(models.TextChoices):
        MAX_GROUPS = 'max_groups', 'Max groups'
        MAX_MEMBERS_PER_GROUP = 'max_members_per_group', 'Max members per group'
        MAX_ADMINS_PER_GROUP = 'max_admins_per_group', 'Max admins per group'
        STORAGE_GB = 'storage_gb', 'Storage (GB)'
        ANALYTICS = 'analytics', 'Analytics'
        CREDIT_PACKAGE = 'credit_package', 'Credit package'

    catalog_version = models.ForeignKey(
        PricingCatalogVersion, on_delete=models.CASCADE, related_name='options',
    )
    category = models.CharField(max_length=32, choices=Category.choices)
    # Matches the frontend configuration's own value type - a limit's numeric
    # tier, an analytics tier code, or a credit package's message count - kept
    # as text so all categories share one column.
    value = models.CharField(max_length=32)
    label = models.CharField(max_length=128)
    price_cents = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['category', 'sort_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['catalog_version', 'category', 'value'],
                name='enterprise_pricingoption_unique_value_per_category',
            )
        ]
        indexes = [models.Index(fields=['catalog_version', 'category', 'is_active'])]

    def __str__(self):
        return f'{self.category}={self.value} ({self.label})'


class PlanFeature(models.Model):
    """One toggleable, priced add-on feature for a given catalog version."""

    catalog_version = models.ForeignKey(
        PricingCatalogVersion, on_delete=models.CASCADE, related_name='features',
    )
    key = models.CharField(max_length=64)
    label = models.CharField(max_length=128)
    description = models.CharField(max_length=255, blank=True, default='')
    price_cents = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['catalog_version', 'key'],
                name='enterprise_planfeature_unique_key_per_catalog',
            )
        ]

    def __str__(self):
        return self.key


class BillingIntervalOption(models.Model):
    """A billing cadence (monthly/quarterly/annual) and its period discount."""

    catalog_version = models.ForeignKey(
        PricingCatalogVersion, on_delete=models.CASCADE, related_name='billing_intervals',
    )
    code = models.CharField(max_length=16)
    label = models.CharField(max_length=32)
    months = models.PositiveSmallIntegerField()
    discount_percent = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['months']
        constraints = [
            models.UniqueConstraint(
                fields=['catalog_version', 'code'],
                name='enterprise_billinginterval_unique_code_per_catalog',
            )
        ]

    def __str__(self):
        return self.code


class EnterpriseBaseFee(models.Model):
    """The recurring fee every Enterprise plan carries before any selection."""

    catalog_version = models.OneToOneField(
        PricingCatalogVersion, on_delete=models.CASCADE, related_name='base_fee',
    )
    amount_cents = models.PositiveIntegerField()

    def __str__(self):
        return f'{self.amount_cents}c base fee (v{self.catalog_version_id})'


class EnterprisePlanDraft(models.Model):
    """A configuration in progress - the Plan Builder's save point.

    Owned by `account` once the caller is signed in, or by `session_key`
    while anonymous - never by `draft_id` alone, which is only an opaque
    handle the frontend already has, not proof of ownership.
    """

    STATUS_CHOICES = [
        ('draft', 'Draft'), ('attached', 'Attached'), ('converted', 'Converted'),
        ('expired', 'Expired'), ('cancelled', 'Cancelled'),
    ]

    draft_id = models.CharField(max_length=40, unique=True, db_index=True)
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='enterprise_drafts',
    )
    session_key = models.CharField(max_length=64, null=True, blank=True)
    configuration = models.JSONField()
    catalog_version = models.ForeignKey(PricingCatalogVersion, on_delete=models.PROTECT)
    calculated_subtotal_cents = models.PositiveIntegerField()
    discount_cents = models.PositiveIntegerField(default=0)
    calculated_total_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=8, default='usd')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='draft')
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['account']),
            models.Index(fields=['session_key']),
        ]

    def __str__(self):
        return self.draft_id


class EnterpriseContract(models.Model):
    """A checked-out, frozen Enterprise plan - never mutated by later catalog
    or draft changes once created."""

    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='enterprise_contracts',
    )
    contract_number = models.CharField(max_length=40, unique=True)
    draft = models.ForeignKey(EnterprisePlanDraft, on_delete=models.PROTECT)
    configuration_snapshot = models.JSONField()
    pricing_snapshot = models.JSONField()
    catalog_version = models.ForeignKey(PricingCatalogVersion, on_delete=models.PROTECT)
    billing_interval = models.CharField(max_length=16)
    status = models.CharField(max_length=16, default='pending')
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['account', '-created_at'])]

    def __str__(self):
        return self.contract_number


class EnterpriseStripeBilling(models.Model):
    """The Stripe-side handles for one EnterpriseContract."""

    contract = models.OneToOneField(
        EnterpriseContract, on_delete=models.CASCADE, related_name='stripe_billing',
    )
    stripe_customer_id = models.CharField(max_length=255)
    stripe_subscription_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_invoice_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_checkout_session_id = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=32, blank=True, default='')

    class Meta:
        indexes = [models.Index(fields=['stripe_subscription_id'])]

    def __str__(self):
        return f'stripe billing for {self.contract.contract_number}'


class EnterpriseEntitlement(models.Model):
    """One granted capability or limit a contract's account is entitled to."""

    contract = models.ForeignKey(
        EnterpriseContract, on_delete=models.CASCADE, related_name='entitlements',
    )
    feature_key = models.CharField(max_length=64)
    enabled = models.BooleanField(default=True)
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    limit_type = models.CharField(max_length=32, blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['contract', 'feature_key'],
                name='enterprise_entitlement_unique_key_per_contract',
            )
        ]

    def __str__(self):
        return f'{self.contract.contract_number}:{self.feature_key}'


class CreditLedgerEntry(models.Model):
    """One movement of a contract's message-credit balance - append-only,
    same shape/reasoning as `billing.WalletTransaction`."""

    TRANSACTION_TYPES = [
        ('allocation', 'Allocation'), ('usage', 'Usage'), ('adjustment', 'Adjustment'),
        ('expiration', 'Expiration'), ('refund', 'Refund'),
    ]

    contract = models.ForeignKey(
        EnterpriseContract, on_delete=models.CASCADE, related_name='credit_entries',
    )
    transaction_type = models.CharField(max_length=16, choices=TRANSACTION_TYPES)
    amount = models.IntegerField()
    balance_after = models.IntegerField()
    reference_type = models.CharField(max_length=32, blank=True, default='')
    reference_id = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['contract', '-created_at'])]

    def __str__(self):
        return f'{self.transaction_type} {self.amount} ({self.contract.contract_number})'
