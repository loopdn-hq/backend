"""A company's money: the only code that writes a balance.

docs/campaigns_final_plan.md, section 9. Two buckets on the company -
AVAILABLE (`balance_cents`) and RESERVED (`reserved_cents`) - and on each
campaign its own reservation and spend. Every movement is one AdTransaction
row recording how both buckets moved and where they stood after, written in
the same transaction as the move.

Each function locks the company row first, so two submits racing for the
same money cannot both have it: the second waits, then sees what the first
left. Callers that also lock a campaign (services/transitions.py) lock the
campaign before calling here - always campaign, then company - so two moves
cannot deadlock each other.
"""
from django.db import transaction

from commercial.models import AdTransaction, Company
from commercial.services.errors import Refused


class InsufficientFunds(Refused):
    """Available money is short. `shortfall_cents` is how much more is needed."""

    def __init__(self, company, needed_cents):
        self.shortfall_cents = needed_cents - company.balance_cents
        super().__init__(
            f'Add {format_money(self.shortfall_cents, company.currency)} to your '
            f'balance to cover this budget. Available now: '
            f'{format_money(company.balance_cents, company.currency)}.'
        )


def format_money(cents, currency):
    """"PHP 1,500.00" - for messages; the dashboard formats amounts itself."""
    return f'{currency} {cents / 100:,.2f}'


def credit_purchase(company, *, amount_cents, stripe_payment_intent_id, description=''):
    """Money in from a cleared Stripe payment: available goes up.

    Idempotent on the payment: a second call for the same payment intent - a
    retried webhook, a replayed event - returns the first row and moves
    nothing. The unique constraint on the ledger is the backstop.
    """
    if amount_cents <= 0:
        raise ValueError('A purchase adds money.')
    with transaction.atomic():
        company = _lock(company)
        existing = AdTransaction.objects.filter(
            kind=AdTransaction.Kind.CREDIT_PURCHASE,
            stripe_payment_intent_id=stripe_payment_intent_id,
        ).first()
        if existing is not None:
            return existing
        company.balance_cents += amount_cents
        company.save(update_fields=['balance_cents', 'updated_at'])
        return _record(
            company,
            AdTransaction.Kind.CREDIT_PURCHASE,
            available=amount_cents,
            description=description or 'Funds added by card',
            stripe_payment_intent_id=stripe_payment_intent_id,
        )


def reserve(campaign, amount_cents, *, description=''):
    """Commit `amount_cents` of available money to `campaign`.

    Raises InsufficientFunds, and moves nothing, if available is short.
    `campaign` must be the caller's locked row; it is saved here.
    """
    if amount_cents <= 0:
        raise ValueError('A reservation sets money aside.')
    with transaction.atomic():
        company = _lock(campaign.company)
        if company.balance_cents < amount_cents:
            raise InsufficientFunds(company, amount_cents)
        company.balance_cents -= amount_cents
        company.reserved_cents += amount_cents
        company.save(update_fields=['balance_cents', 'reserved_cents', 'updated_at'])
        campaign.company = company
        campaign.reserved_cents += amount_cents
        campaign.save(update_fields=['reserved_cents', 'updated_at'])
        return _record(
            company,
            AdTransaction.Kind.CAMPAIGN_RESERVE,
            campaign=campaign,
            available=-amount_cents,
            reserved=amount_cents,
            description=description or f'Budget set aside for {campaign}',
        )


def release(campaign, *, description=''):
    """Return everything `campaign` has reserved and not spent to available.

    Nothing to return is not an error: it moves nothing and writes no row.
    `campaign` must be the caller's locked row; it is saved here.
    """
    amount_cents = campaign.reserved_cents
    if amount_cents <= 0:
        return None
    with transaction.atomic():
        company = _lock(campaign.company)
        company.balance_cents += amount_cents
        company.reserved_cents -= amount_cents
        company.save(update_fields=['balance_cents', 'reserved_cents', 'updated_at'])
        campaign.company = company
        campaign.reserved_cents = 0
        campaign.save(update_fields=['reserved_cents', 'updated_at'])
        return _record(
            company,
            AdTransaction.Kind.CAMPAIGN_RELEASE,
            campaign=campaign,
            available=amount_cents,
            reserved=-amount_cents,
            description=description or f'Budget returned from {campaign}',
        )


def _lock(company):
    return Company.objects.select_for_update().get(pk=company.pk)


def _record(company, kind, *, campaign=None, available=0, reserved=0, description='',
            stripe_payment_intent_id=''):
    return AdTransaction.objects.create(
        company=company,
        campaign=campaign,
        kind=kind,
        amount_cents=available,
        balance_after_cents=company.balance_cents,
        reserved_change_cents=reserved,
        reserved_after_cents=company.reserved_cents,
        description=description[:200],
        stripe_payment_intent_id=stripe_payment_intent_id,
    )
