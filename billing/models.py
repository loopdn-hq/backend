from django.conf import settings
from django.db import models


class Plan(models.Model):
    """One row per platform subscription tier: Free, Premium, Business, Enterprise.

    Prices and limits live here rather than in code so that changing what
    Premium includes is an admin edit, not a deploy. `monthly_price_cents` is
    nullable for Enterprise, which is quoted, not charged automatically -
    `is_custom_pricing` is what a client checks before showing a price at all.
    """

    class Code(models.TextChoices):
        FREE = 'free', 'Free'
        PREMIUM = 'premium', 'Premium'
        BUSINESS = 'business', 'Business'
        ENTERPRISE = 'enterprise', 'Enterprise'

    code = models.CharField(max_length=16, choices=Code.choices, unique=True)
    name = models.CharField(max_length=40)
    monthly_price_cents = models.PositiveIntegerField(null=True, blank=True)

    # Stripe's recurring Price for this plan. Blank until
    # `sync_stripe_billing` (or the dashboard) creates the Product/Price and
    # somebody pastes the id back - Free and Enterprise never get one.
    stripe_price_id = models.CharField(max_length=255, blank=True, default='', db_default='')

    # Usage limits. Null means "no limit" (Enterprise), never zero.
    max_groups = models.PositiveIntegerField(null=True, blank=True)
    max_members_per_group = models.PositiveIntegerField(null=True, blank=True)
    max_admins_per_group = models.PositiveIntegerField(null=True, blank=True)
    storage_gb = models.PositiveIntegerField(null=True, blank=True)

    has_whatsapp = models.BooleanField(default=False, db_default=False)
    has_telegram = models.BooleanField(default=False, db_default=False)

    # Free-form marketing bullet points (e.g. "Advanced moderation tools"),
    # shown as-is by a pricing page. Not read by any enforcement code - the
    # typed fields above are, so a limit can never drift from what a client
    # displays.
    features = models.JSONField(default=list, blank=True)

    is_custom_pricing = models.BooleanField(default=False, db_default=False)
    active = models.BooleanField(default=True, db_default=True)
    sort_order = models.PositiveSmallIntegerField(default=0, db_default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.name


class CreditPackage(models.Model):
    """A pre-priced Communication Credit top-up (Starter, Standard, ...).

    `credit_cents` is what lands in the wallet, `price_cents` is what Stripe
    charges - kept as two fields, rather than one price the wallet is assumed
    to match, so a promotional package (pay $10, get $12 of credit) is just a
    row where the two differ, not a special case in code.
    """

    name = models.CharField(max_length=40)
    price_cents = models.PositiveIntegerField()
    credit_cents = models.PositiveIntegerField()

    # Stripe's one-time Price for this package. Blank until synced - see
    # Plan.stripe_price_id above for why.
    stripe_price_id = models.CharField(max_length=255, blank=True, default='', db_default='')

    active = models.BooleanField(default=True, db_default=True)
    sort_order = models.PositiveSmallIntegerField(default=0, db_default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.name


class Subscription(models.Model):
    """A user's standing with a Plan - what Stripe calls a subscription.

    One per user: `user` is a `OneToOneField`, so upgrading is an update to
    this row rather than a new one, and "what plan is this person on" is
    always a single lookup with no "which is the active one" ambiguity.
    Created eagerly (defaulting to the Free plan) so every user has one from
    signup, rather than "no row" meaning free - which would make every limit
    check handle a null subscription as a special case.
    """

    class Status(models.TextChoices):
        FREE = 'free', 'Free'
        ACTIVE = 'active', 'Active'
        TRIALING = 'trialing', 'Trialing'
        PAST_DUE = 'past_due', 'Past due'
        CANCELED = 'canceled', 'Canceled'
        INCOMPLETE = 'incomplete', 'Incomplete'
        UNPAID = 'unpaid', 'Unpaid'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='subscription',
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='subscriptions')

    stripe_customer_id = models.CharField(max_length=255, blank=True, default='', db_default='')
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default='', db_default='')
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.FREE, db_default=Status.FREE
    )
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False, db_default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['stripe_customer_id'])]

    def __str__(self):
        return f'{self.user_id}:{self.plan.code}'

    def is_active(self):
        """Whether this grants the plan's limits right now.

        A canceled subscription still shows its old plan until Stripe's
        period actually ends - `cancel_at_period_end` - so callers check this
        rather than `status == ACTIVE` directly.
        """
        return self.status in (self.Status.FREE, self.Status.ACTIVE, self.Status.TRIALING)


class Wallet(models.Model):
    """A user's Communication Credit balance - the PAYG side of billing.

    Kept entirely separate from `Subscription`: a Free user with no paid plan
    can still hold credits and send SMS through them, per the pricing model's
    "dual-wallet" design. Balance is in cents so `$0.015` per SMS part is
    exact integer arithmetic, never floats.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='wallet',
    )
    balance_cents = models.IntegerField(default=0, db_default=0)

    auto_recharge_enabled = models.BooleanField(default=False, db_default=False)
    # Which package to buy automatically once the balance runs low. Optional -
    # SET_NULL, not CASCADE, so retiring a package does not silently disable
    # every customer's auto-recharge, it just leaves them with none picked.
    auto_recharge_package = models.ForeignKey(
        CreditPackage, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # The ledger (WalletTransaction) is the source of truth for how
            # balance_cents got here; this constraint is just the backstop
            # that stops a bug from ever taking it negative.
            models.CheckConstraint(
                condition=models.Q(balance_cents__gte=0),
                name='wallet_balance_non_negative',
            )
        ]

    def __str__(self):
        return f'{self.user_id}: {self.balance_cents}c'


class WalletTransaction(models.Model):
    """One movement of a wallet's balance - the append-only ledger.

    `balance_after_cents` is stored rather than computed, so a support ticket
    ("why is my balance $2.50") is answered by one row, not a running sum over
    the whole history. Written inside the same transaction as the balance
    update it describes - see `billing/services.py`.
    """

    class Kind(models.TextChoices):
        CREDIT_PURCHASE = 'credit_purchase', 'Credit purchase'
        USAGE_DEBIT = 'usage_debit', 'Usage debit'
        ADJUSTMENT = 'adjustment', 'Adjustment'

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # Signed: positive for a purchase or credit adjustment, negative for usage.
    amount_cents = models.IntegerField()
    balance_after_cents = models.IntegerField()
    description = models.CharField(max_length=200, blank=True, default='', db_default='')
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default='', db_default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['wallet', '-created_at'])]

    def __str__(self):
        return f'{self.kind} {self.amount_cents}c'


class StripeWebhookEvent(models.Model):
    """One Stripe event we have already processed.

    Stripe retries any webhook response it does not read as success, so this is
    what stops a retried `checkout.session.completed` (or any other event) from
    being handled twice. Keyed on Stripe's own event id, which is unique per
    event and stable across retries of the same delivery.
    """

    event_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=64, blank=True, default='', db_default='')
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [models.Index(fields=['received_at'])]

    def __str__(self):
        return f'{self.event_type or "event"} {self.event_id}'
