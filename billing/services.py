"""Business logic for plans, subscriptions and the communication wallet.

Kept apart from the viewsets (thin, HTTP-shaped) and the webhook (thin,
Stripe-event-shaped) so both call the same functions and cannot drift apart -
a checkout completing and a subscription being restored by support hit the
same `activate_subscription`, for instance.
"""
import hashlib
import logging
import time

import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing.models import Payment, Plan, Subscription, Wallet, WalletTransaction

logger = logging.getLogger(__name__)


class InsufficientCredit(Exception):
    """The wallet does not have enough balance to cover this debit."""


def _idempotency_key(*parts, bucket_seconds):
    """A Stripe idempotency key, stable for `bucket_seconds` then rotating.

    Stripe replays the FIRST response for a given key rather than repeating
    the side effect, so a double-click, a client retry after a timeout, or a
    dropped connection right after Stripe received the request all land on
    the same Checkout Session / customer / portal session instead of a second
    one. Bucketed by time rather than fixed forever, so a genuine later
    attempt (the user actually changed their mind and tried again) is not
    silently handed back a stale, possibly-expired session.
    """
    bucket = int(time.time() // bucket_seconds)
    raw = ':'.join(str(part) for part in (*parts, bucket))
    return hashlib.sha256(raw.encode()).hexdigest()


def get_free_plan():
    """The Free plan, creating it if the seed row is somehow missing.

    Normally seeded by `0002_seed_plans_and_credit_packages`, but every
    subscription defaults to this plan (`get_or_create_subscription`), which
    makes it a hard dependency of something as basic as creating a group -
    `.get()` alone would turn a missing/deleted row into a 500 on totally
    unrelated features rather than the admin action of restoring a plan.
    `get_or_create` also survives `TransactionTestCase`, which truncates
    every table (including data-migration rows) after each test and does not
    restore them.
    """
    plan, _ = Plan.objects.get_or_create(
        code=Plan.Code.FREE,
        defaults={
            'name': 'Free', 'monthly_price_cents': 0,
            'max_groups': 3, 'max_members_per_group': 20, 'max_admins_per_group': 1,
            'storage_gb': 1,
            'max_call_participants': 4,
            'shows_ads': True,
        },
    )
    return plan


def get_or_create_subscription(user):
    """Every user has exactly one of these, defaulting to Free.

    Called lazily rather than on signup, so a Free plan row missing (a fresh
    database that has not been seeded yet) fails loudly here instead of
    silently at registration.
    """
    subscription, _ = Subscription.objects.get_or_create(
        user=user, defaults={'plan': get_free_plan()}
    )
    return subscription


def get_or_create_wallet(user):
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


def get_or_create_stripe_customer(user):
    """The Stripe Customer standing in for this user, creating one if needed.

    Stored on `Subscription` rather than `Profile`: a customer id is a
    billing fact, and billing not being enabled locally (no secret key) must
    not force every Profile row to carry an empty Stripe field.

    Verifies the stored id still resolves on Stripe's side before reusing it -
    a customer deleted from the dashboard (test cleanup, a GDPR request)
    otherwise leaves a permanently dead id behind, and every later checkout
    for that user fails with "No such customer" until somebody notices.
    """
    subscription = get_or_create_subscription(user)
    if subscription.stripe_customer_id:
        try:
            stripe.Customer.retrieve(subscription.stripe_customer_id)
            return subscription.stripe_customer_id
        except stripe.InvalidRequestError:
            logger.warning(
                'Stripe customer %s for user %s no longer exists; creating a new one',
                subscription.stripe_customer_id, user.pk,
            )

    customer = stripe.Customer.create(
        email=user.email or None,
        name=user.get_username(),
        metadata={'user_id': str(user.pk)},
        # A narrow window - just wide enough to survive a genuine retry of
        # THIS request, not to block a customer legitimately created again
        # after this one was deleted (see the retrieve() check above it).
        idempotency_key=_idempotency_key('customer', user.pk, bucket_seconds=60),
    )
    subscription.stripe_customer_id = customer['id']
    subscription.save(update_fields=['stripe_customer_id', 'updated_at'])
    return customer['id']


def create_plan_checkout_session(user, plan, success_url, cancel_url):
    """A Checkout Session that subscribes `user` to `plan` on completion.

    Only for plans that charge through Stripe - Free has no price to check
    out with, and Enterprise is quoted by sales, not self-served.
    """
    if not plan.stripe_price_id:
        raise ValueError(f'Plan {plan.code} has no Stripe price configured.')

    customer_id = get_or_create_stripe_customer(user)
    return stripe.checkout.Session.create(
        mode='subscription',
        customer=customer_id,
        line_items=[{'price': plan.stripe_price_id, 'quantity': 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user.pk),
        metadata={'user_id': str(user.pk), 'plan_code': plan.code},
        # 5 minutes: long enough that a double-click or a client retrying a
        # timed-out request reuses this session rather than opening a second
        # one somebody could also pay, short enough that abandoning this one
        # and deliberately starting over shortly after still gets a fresh session.
        idempotency_key=_idempotency_key(
            'checkout-plan', user.pk, plan.code, bucket_seconds=300
        ),
    )


def create_credit_checkout_session(user, package, success_url, cancel_url):
    """A Checkout Session that tops up the wallet by `package` on completion.

    A one-time payment, not a subscription - PAYG credits are bought again
    each time, unlike the plan itself.
    """
    if not package.stripe_price_id:
        raise ValueError(f'Credit package {package.pk} has no Stripe price configured.')

    customer_id = get_or_create_stripe_customer(user)
    return stripe.checkout.Session.create(
        mode='payment',
        customer=customer_id,
        line_items=[{'price': package.stripe_price_id, 'quantity': 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user.pk),
        metadata={'user_id': str(user.pk), 'credit_package_id': str(package.pk)},
        # Same reasoning as the plan checkout above.
        idempotency_key=_idempotency_key(
            'checkout-credits', user.pk, package.pk, bucket_seconds=300
        ),
    )


def create_billing_portal_session(user, return_url):
    """Stripe's hosted page for managing payment methods and cancelling."""
    customer_id = get_or_create_stripe_customer(user)
    return stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
        idempotency_key=_idempotency_key('portal', user.pk, bucket_seconds=60),
    )


@transaction.atomic
def credit_wallet(user, *, amount_cents, kind, description='', stripe_payment_intent_id=''):
    """Add to a wallet's balance and append the ledger row that explains it.

    `select_for_update` because a webhook retry racing a second, genuine
    purchase must not read a stale balance and overwrite the other's update.
    """
    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    wallet.balance_cents += amount_cents
    wallet.save(update_fields=['balance_cents', 'updated_at'])
    WalletTransaction.objects.create(
        wallet=wallet,
        kind=kind,
        amount_cents=amount_cents,
        balance_after_cents=wallet.balance_cents,
        description=description,
        stripe_payment_intent_id=stripe_payment_intent_id,
    )
    return wallet


@transaction.atomic
def debit_wallet(user, *, amount_cents, description=''):
    """Spend from a wallet - e.g. an SMS part sent through Telnyx.

    Raises rather than going negative: the database constraint is the last
    line of defence, this is the one callers are meant to actually handle
    (refuse to send, or fall back to a subscription-included allowance).
    """
    if amount_cents <= 0:
        raise ValueError('amount_cents must be positive; debit_wallet subtracts it itself.')

    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    if wallet.balance_cents < amount_cents:
        raise InsufficientCredit(
            f'Wallet for user {user.pk} has {wallet.balance_cents}c, needs {amount_cents}c.'
        )
    wallet.balance_cents -= amount_cents
    wallet.save(update_fields=['balance_cents', 'updated_at'])
    WalletTransaction.objects.create(
        wallet=wallet,
        kind=WalletTransaction.Kind.USAGE_DEBIT,
        amount_cents=-amount_cents,
        balance_after_cents=wallet.balance_cents,
        description=description,
    )
    return wallet


@transaction.atomic
def activate_subscription(user, *, plan_code, stripe_subscription_id, status, current_period_end,
                           cancel_at_period_end=False):
    """Point a user's Subscription row at a (new) plan and Stripe status.

    Shared by the webhook's `checkout.session.completed` and
    `customer.subscription.updated` handlers, since both are "this is what
    Stripe says is true now" - the only difference is which fields Stripe
    included in that particular event.
    """
    subscription = get_or_create_subscription(user)
    subscription.plan = Plan.objects.get(code=plan_code)
    subscription.stripe_subscription_id = stripe_subscription_id
    subscription.status = status
    subscription.current_period_end = current_period_end
    subscription.cancel_at_period_end = cancel_at_period_end
    subscription.save()
    return subscription


@transaction.atomic
def downgrade_to_free(user):
    """A subscription ended (canceled, or payment finally failed for good)."""
    subscription = get_or_create_subscription(user)
    subscription.plan = get_free_plan()
    subscription.status = Subscription.Status.FREE
    subscription.stripe_subscription_id = ''
    subscription.current_period_end = None
    subscription.cancel_at_period_end = False
    subscription.save()
    return subscription


def plan_code_for_stripe_price(price_id):
    """Which of our Plans a Stripe price id belongs to, for a webhook payload
    that gives us the price but not which plan we meant it as."""
    plan = Plan.objects.filter(stripe_price_id=price_id).first()
    return plan.code if plan else None


def credit_package_for_stripe_price(price_id):
    from billing.models import CreditPackage
    return CreditPackage.objects.filter(stripe_price_id=price_id).first()


def stripe_timestamp_to_datetime(value):
    if value is None:
        return None
    return timezone.datetime.fromtimestamp(value, tz=timezone.get_current_timezone())


def record_payment(user, *, kind, amount_cents, stripe_reference, currency='usd',
                   description='', receipt_url='', paid_at=None):
    """Append one successful charge to the user's transaction history.

    Keyed on Stripe's own id for the payment, so the webhook hearing about an
    invoice and the backfill finding the same invoice later leave one row.
    """
    payment, _ = Payment.objects.get_or_create(
        stripe_reference=stripe_reference,
        defaults={
            'user': user,
            'kind': kind,
            'amount_cents': amount_cents,
            'currency': (currency or 'usd').lower(),
            'description': description[:200],
            'receipt_url': receipt_url or '',
            'paid_at': paid_at or timezone.now(),
        },
    )
    return payment


def _invoice_subscription_id(invoice):
    """The subscription an invoice bills. Stripe moved it off the Invoice
    itself and under `parent` in newer API versions; read both."""
    subscription_id = invoice.get('subscription')
    if not subscription_id:
        details = (invoice.get('parent') or {}).get('subscription_details') or {}
        subscription_id = details.get('subscription')
    return subscription_id if isinstance(subscription_id, str) else None


def _invoice_first_line(invoice):
    lines = (invoice.get('lines') or {}).get('data') or []
    return lines[0] if lines else {}


def _invoice_price_id(invoice):
    """The price on an invoice's first line - same move as above: `price` on
    the line in older API versions, under `pricing` in newer ones."""
    line = _invoice_first_line(invoice)
    price = line.get('price')
    if isinstance(price, dict):
        return price.get('id')
    return ((line.get('pricing') or {}).get('price_details') or {}).get('price')


def record_invoice_payment(invoice):
    """Record a paid subscription invoice: a plan's, or an Enterprise contract's.

    Returns None for an invoice with nothing charged (a trial, a fully
    discounted period) or one whose customer is nobody we know.
    """
    # Deferred: `enterprise.services` imports this module at its top level.
    from enterprise.models import EnterpriseStripeBilling

    invoice_id = invoice.get('id')
    amount_cents = invoice.get('amount_paid') or 0
    if not invoice_id or amount_cents <= 0:
        return None

    subscription_id = _invoice_subscription_id(invoice)
    enterprise_billing = EnterpriseStripeBilling.objects.filter(
        stripe_subscription_id=subscription_id
    ).select_related('contract__account').first() if subscription_id else None

    if enterprise_billing is not None:
        contract = enterprise_billing.contract
        user = contract.account
        kind = Payment.Kind.ENTERPRISE_PLAN
        description = f'Enterprise plan ({contract.contract_number})'
    else:
        customer_id = invoice.get('customer')
        subscription = Subscription.objects.filter(
            stripe_customer_id=customer_id
        ).select_related('user').first() if customer_id else None
        if subscription is None:
            logger.error('Paid Stripe invoice %s with no resolvable user', invoice_id)
            return None
        user = subscription.user
        kind = Payment.Kind.PLAN_SUBSCRIPTION
        price_id = _invoice_price_id(invoice)
        plan = Plan.objects.filter(stripe_price_id=price_id).first() if price_id else None
        description = (
            f'{plan.name} plan' if plan is not None
            else _invoice_first_line(invoice).get('description') or 'Plan subscription'
        )

    paid_at = (invoice.get('status_transitions') or {}).get('paid_at') or invoice.get('created')
    return record_payment(
        user,
        kind=kind,
        amount_cents=amount_cents,
        currency=invoice.get('currency'),
        description=description,
        stripe_reference=invoice_id,
        receipt_url=invoice.get('hosted_invoice_url') or invoice.get('invoice_pdf') or '',
        paid_at=stripe_timestamp_to_datetime(paid_at),
    )


def mark_enterprise_invoice(invoice_id, contract_number):
    """Relabel an invoice's payment as its Enterprise contract's.

    `invoice.paid` can arrive before the `checkout.session.completed` that
    ties the subscription to its contract; the payment is then recorded as an
    ordinary plan's. This is the checkout catching up.
    """
    if not invoice_id:
        return
    Payment.objects.filter(stripe_reference=invoice_id).update(
        kind=Payment.Kind.ENTERPRISE_PLAN,
        description=f'Enterprise plan ({contract_number})',
    )
