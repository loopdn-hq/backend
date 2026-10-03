from django.db import models
from django.db.models import Q

from commercial.markets import Currency


class PricingRate(models.Model):
    """What 1,000 viewed ads cost, in one placement and currency, from a date.

    docs/campaigns_final_plan.md, section 9. Never hard-coded: a price change is
    a new row with a later `effective_from`, not an edit, so the history of
    what was charged when stays readable. The rate in force is copied onto a
    campaign when it is submitted (`Campaign.cpm_cents`), so a later change
    never alters a campaign already bought.

    CPM only for now. Per click, per lead or per call would be a new `kind`
    when they arrive - the plan starts with one.
    """

    placement = models.CharField(max_length=16)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    # Minor units per 1,000 viewed ads: 5000 is ₱50.00 per 1,000.
    cpm_cents = models.PositiveIntegerField()
    effective_from = models.DateTimeField()
    note = models.CharField(max_length=200, blank=True, default='', db_default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['placement', 'currency', '-effective_from']
        constraints = [
            models.CheckConstraint(
                condition=Q(cpm_cents__gt=0), name='commercial_pricingrate_positive'
            ),
            models.UniqueConstraint(
                fields=['placement', 'currency', 'effective_from'],
                name='commercial_pricingrate_one_per_moment',
            ),
        ]

    def __str__(self):
        return f'{self.placement} {self.currency} {self.cpm_cents}/1000 from {self.effective_from:%Y-%m-%d}'
