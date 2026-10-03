"""What viewed ads cost: the rate in force, and what a budget buys.

docs/campaigns_final_plan.md, section 9. Prices are PricingRate rows, never
constants in code. Submit copies the rate in force onto the campaign
(`Campaign.cpm_cents`); everything after that reads the campaign's own copy.
"""
from django.utils import timezone

from commercial.models import PricingRate


def rate_in_force(placement, currency, at=None):
    """The PricingRate that applies now (or at `at`), or None if there is none."""
    return (
        PricingRate.objects.filter(
            placement=placement,
            currency=currency,
            effective_from__lte=at or timezone.now(),
        )
        .order_by('-effective_from', '-pk')
        .first()
    )


def views_for(budget_cents, cpm_cents):
    """About how many viewed ads `budget_cents` buys at `cpm_cents` per 1,000."""
    if not budget_cents or not cpm_cents:
        return 0
    return budget_cents * 1000 // cpm_cents
