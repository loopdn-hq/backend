from django.db import models
from django.db.models import Q


class AdTransaction(models.Model):
    """One movement of a company's balance - the append-only ledger.

    The same shape and the same words as `billing.WalletTransaction`, plus
    `refund`, so the two can later share a base class and service code without
    either ever borrowing the other's table. `balance_after_cents` is stored for
    the reason it is stored there: "why is my balance ₱250" is one row, not a
    sum over the whole history.

    The sign of `amount_cents` is fixed per kind, and the database checks it:

        credit_purchase   +   money in, from a Stripe payment
        usage_debit       -   what one campaign spent; `campaign` is required
        refund            -   money paid back out to the company's card
        adjustment       +/-  a correction by Loopd'n staff, with a description

    Write `usage_debit` per campaign per billing period, not per impression. The
    per-delivery figure is `AdImpression.cost_cents`; a ledger row for every
    delivery would outgrow every other table in the app.

    Every foreign key is PROTECT: this is a financial record, and deleting a
    campaign or a company must fail rather than take its spending with it.
    """

    class Kind(models.TextChoices):
        CREDIT_PURCHASE = 'credit_purchase', 'Credit purchase'
        USAGE_DEBIT = 'usage_debit', 'Usage debit'
        REFUND = 'refund', 'Refund'
        ADJUSTMENT = 'adjustment', 'Adjustment'

    company = models.ForeignKey(
        'commercial.Company', on_delete=models.PROTECT, related_name='transactions'
    )
    # Set on every usage_debit, which is what makes cost per result by campaign
    # a query rather than an estimate.
    campaign = models.ForeignKey(
        'commercial.Campaign',
        on_delete=models.PROTECT,
        related_name='transactions',
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    amount_cents = models.IntegerField()
    balance_after_cents = models.IntegerField()
    description = models.CharField(max_length=200, blank=True, default='', db_default='')
    stripe_payment_intent_id = models.CharField(
        max_length=255, blank=True, default='', db_default=''
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind='credit_purchase', amount_cents__gt=0)
                    | Q(kind__in=['usage_debit', 'refund'], amount_cents__lt=0)
                    | (Q(kind='adjustment') & ~Q(amount_cents=0))
                ),
                name='commercial_adtransaction_sign_matches_kind',
            ),
            models.CheckConstraint(
                condition=~Q(kind='usage_debit') | Q(campaign__isnull=False),
                name='commercial_adtransaction_usage_debit_names_campaign',
            ),
            models.CheckConstraint(
                condition=Q(balance_after_cents__gte=0),
                name='commercial_adtransaction_balance_after_non_negative',
            ),
            # A Stripe webhook retried after it was handled must not credit the
            # same payment twice. Refunds may share the intent id, so only
            # purchases are constrained.
            models.UniqueConstraint(
                fields=['stripe_payment_intent_id'],
                condition=Q(kind='credit_purchase') & ~Q(stripe_payment_intent_id=''),
                name='commercial_adtransaction_one_credit_per_payment',
            ),
        ]
        indexes = [
            models.Index(fields=['company', '-created_at'], name='commercial_txn_company_idx'),
            models.Index(fields=['campaign', 'kind'], name='commercial_txn_campaign_idx'),
        ]

    def __str__(self):
        return f'{self.kind} {self.amount_cents}c'
