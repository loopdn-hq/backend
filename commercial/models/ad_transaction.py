from django.db import models
from django.db.models import F, Q


class AdTransaction(models.Model):
    """One movement of a company's money - the append-only ledger.

    docs/campaigns_final_plan.md, section 9. A company has two buckets:
    AVAILABLE (`Company.balance_cents`), free to commit to a new campaign, and
    RESERVED (`Company.reserved_cents`), committed to submitted campaigns and
    not yet spent. Every row says how each bucket moved and where both stood
    afterwards, so "why is my balance ₱250" is one row, not a sum over the
    whole history. services/wallet.py is the only thing that writes here.

        kind               available   reserved   when
        credit_purchase        +                  a Stripe payment clears
        campaign_reserve       -          +       submit; a budget increase
        campaign_spend                    -       settlement (slice 5)
        campaign_release       +          -       withdraw, reject, cancel, end
        refund                 -                  money paid back to the card
        adjustment            +/-                 a staff correction, with a reason

    `amount_cents` is the change to AVAILABLE and `balance_after_cents` where
    available stood after it - the names `billing.WalletTransaction` uses, so
    the two read alike. `reserved_change_cents` and `reserved_after_cents` are
    the same for RESERVED. The database checks every sign.

    Write `campaign_spend` per campaign per settlement, not per view: the
    per-view figure lives on the impression, and a ledger row for every view
    would outgrow every other table in the app.

    Every foreign key is PROTECT: this is a financial record, and deleting a
    campaign or a company must fail rather than take its money with it.
    """

    class Kind(models.TextChoices):
        CREDIT_PURCHASE = 'credit_purchase', 'Funds added'
        CAMPAIGN_RESERVE = 'campaign_reserve', 'Budget set aside'
        CAMPAIGN_SPEND = 'campaign_spend', 'Spent'
        CAMPAIGN_RELEASE = 'campaign_release', 'Budget returned'
        REFUND = 'refund', 'Refund'
        ADJUSTMENT = 'adjustment', 'Adjustment'

    company = models.ForeignKey(
        'commercial.Company', on_delete=models.PROTECT, related_name='transactions'
    )
    # Set on every row that belongs to a campaign, which is what makes spend
    # and cost per result by campaign a query rather than an estimate.
    campaign = models.ForeignKey(
        'commercial.Campaign',
        on_delete=models.PROTECT,
        related_name='transactions',
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # The change to AVAILABLE, and where it stood after.
    amount_cents = models.IntegerField()
    balance_after_cents = models.IntegerField()
    # The change to RESERVED, and where it stood after.
    reserved_change_cents = models.IntegerField(default=0, db_default=0)
    reserved_after_cents = models.IntegerField(default=0, db_default=0)
    description = models.CharField(max_length=200, blank=True, default='', db_default='')
    stripe_payment_intent_id = models.CharField(
        max_length=255, blank=True, default='', db_default=''
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind='credit_purchase', amount_cents__gt=0, reserved_change_cents=0)
                    | Q(
                        kind='campaign_reserve',
                        amount_cents__lt=0,
                        reserved_change_cents=-F('amount_cents'),
                    )
                    | Q(kind='campaign_spend', amount_cents=0, reserved_change_cents__lt=0)
                    | Q(
                        kind='campaign_release',
                        amount_cents__gt=0,
                        reserved_change_cents=-F('amount_cents'),
                    )
                    | Q(kind='refund', amount_cents__lt=0, reserved_change_cents=0)
                    | (
                        Q(kind='adjustment', reserved_change_cents=0)
                        & ~Q(amount_cents=0)
                    )
                ),
                name='commercial_adtransaction_movement_matches_kind',
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(kind__in=['campaign_reserve', 'campaign_spend', 'campaign_release'])
                    | Q(campaign__isnull=False)
                ),
                name='commercial_adtransaction_campaign_rows_name_it',
            ),
            models.CheckConstraint(
                condition=Q(balance_after_cents__gte=0) & Q(reserved_after_cents__gte=0),
                name='commercial_adtransaction_buckets_non_negative',
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
        return f'{self.kind} {self.amount_cents}c / reserved {self.reserved_change_cents}c'
