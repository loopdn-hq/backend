"""Adding funds: Stripe Checkout for a company, and crediting it once paid.

docs/campaigns_final_plan.md, section 9. Reuses billing's patterns - its
time-bucketed idempotency keys, its one webhook endpoint and its
`StripeWebhookEvent` de-duplication - but the company has its OWN Stripe
customer (`Company.stripe_customer_id`), not its owner's: receipts carry the
business's name, and the card on file is the business's.

The money moves only when Stripe says the payment cleared:
billing/webhooks.py hands a completed checkout of ours to
`handle_checkout_completed`, which credits through services/wallet.py.
Returning to the success page proves nothing and moves nothing.
"""
import logging

import stripe

from billing.services import _idempotency_key
from commercial.markets import MAX_TOP_UP_CENTS, MIN_TOP_UP_CENTS, Currency
from commercial.models import Company
from commercial.services import wallet
from commercial.services.errors import Invalid

logger = logging.getLogger(__name__)

#: Marks a Checkout Session as a company top-up, for the shared webhook.
CHECKOUT_KIND = 'commercial_funds'


def check_top_up(company, amount_cents):
    """Raise Invalid unless `amount_cents` is a top-up this company may make."""
    least = MIN_TOP_UP_CENTS[Currency(company.currency)]
    if amount_cents < least:
        raise Invalid({
            'amount_cents': f'Add at least {wallet.format_money(least, company.currency)}.'
        })
    if amount_cents > MAX_TOP_UP_CENTS:
        raise Invalid({
            'amount_cents': (
                f'Add at most {wallet.format_money(MAX_TOP_UP_CENTS, company.currency)} '
                'in one payment.'
            )
        })


def create_funds_checkout(company, *, amount_cents, success_url, cancel_url):
    """A Stripe Checkout Session that, once paid, adds `amount_cents` to available.

    The amount is the company's own choice, so the line item is built here
    (`price_data`) rather than picked from a catalogue of Stripe prices.
    """
    check_top_up(company, amount_cents)
    customer_id = company_customer(company)
    return stripe.checkout.Session.create(
        mode='payment',
        customer=customer_id,
        line_items=[
            {
                'price_data': {
                    # Stripe wants currencies in lower case; ours are ISO upper.
                    'currency': company.currency.lower(),
                    'unit_amount': amount_cents,
                    'product_data': {'name': f"Loopd'n Commercial balance · {company.name}"},
                },
                'quantity': 1,
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            'kind': CHECKOUT_KIND,
            'company_id': str(company.pk),
            'amount_cents': str(amount_cents),
        },
        # A double-click or a retried request lands on the same session rather
        # than a second one somebody could also pay - billing's reasoning.
        idempotency_key=_idempotency_key(
            'commercial-funds', company.pk, amount_cents, bucket_seconds=300
        ),
    )


def company_customer(company):
    """The Stripe Customer for this company, creating one the first time.

    Checked before reuse, as billing does for a person: a customer deleted
    from the Stripe dashboard would otherwise fail every later checkout.
    """
    if company.stripe_customer_id:
        try:
            stripe.Customer.retrieve(company.stripe_customer_id)
            return company.stripe_customer_id
        except stripe.InvalidRequestError:
            logger.warning(
                'Stripe customer %s for company %s no longer exists; creating a new one',
                company.stripe_customer_id, company.pk,
            )

    customer = stripe.Customer.create(
        name=company.name,
        email=company.contact_email or None,
        metadata={'company_id': str(company.pk)},
        idempotency_key=_idempotency_key('commercial-customer', company.pk, bucket_seconds=60),
    )
    Company.objects.filter(pk=company.pk).update(stripe_customer_id=customer['id'])
    company.stripe_customer_id = customer['id']
    return customer['id']


def is_funds_checkout(session):
    return (session.get('metadata') or {}).get('kind') == CHECKOUT_KIND


def handle_checkout_completed(session):
    """Credit the company a completed, PAID top-up. Called by billing/webhooks.py.

    Credits what Stripe says was paid (`amount_total`), in the company's own
    currency - a mismatch is logged and credited to nobody, never converted.
    A session still awaiting an asynchronous payment is ignored here; its
    `checkout.session.async_payment_succeeded` event comes back to this
    function once it clears.
    """
    metadata = session.get('metadata') or {}
    company = Company.objects.filter(pk=metadata.get('company_id')).first()
    if company is None:
        logger.error('Funds checkout %s names no known company', session.get('id'))
        return

    if session.get('payment_status') != 'paid':
        logger.info('Funds checkout %s not paid yet (%s)', session.get('id'),
                    session.get('payment_status'))
        return

    currency = (session.get('currency') or '').upper()
    if currency != company.currency:
        logger.error(
            'Funds checkout %s paid in %s, but company %s is in %s; not credited',
            session.get('id'), currency, company.pk, company.currency,
        )
        return

    amount_cents = session.get('amount_total') or 0
    if amount_cents <= 0:
        logger.error('Funds checkout %s has no amount; not credited', session.get('id'))
        return

    wallet.credit_purchase(
        company,
        amount_cents=amount_cents,
        # The payment, not the session, is what must never be credited twice.
        stripe_payment_intent_id=session.get('payment_intent') or session.get('id') or '',
        description='Funds added by card',
    )
