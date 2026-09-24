"""Catalog-driven pricing, drafts, checkout and Stripe-event handling for the
Enterprise Plan Builder.

Mirrors the shape of `billing/services.py`: thin views/webhook call these
functions so a checkout completing and a webhook replaying it hit the same
code. The one rule everything here exists to enforce is that a price never
comes from the client - every dollar amount is looked up from the active
`PricingCatalogVersion` (or, for an existing draft/contract, the specific
version it was priced against) and recomputed from scratch every time.
"""
import logging
import secrets
from datetime import timedelta

import stripe
from django.db import IntegrityError, transaction
from django.utils import timezone

from billing import services as billing_services
from billing.models import Plan, Subscription
from enterprise.models import (
    CreditLedgerEntry,
    EnterpriseContract,
    EnterpriseEntitlement,
    EnterprisePlanDraft,
    EnterpriseStripeBilling,
    PricingCatalogVersion,
)

logger = logging.getLogger(__name__)

LIMIT_CATEGORIES = ['max_groups', 'max_members_per_group', 'max_admins_per_group', 'storage_gb']
FEATURE_KEYS = [
    'extended_message_history', 'search', 'advanced_notifications', 'moderation_tools',
    # Calling. Each is its own line item, priced separately - see
    # `_sync_account_plan` for how they become `billing.Plan` flags.
    'voice_calls', 'video_calls', 'call_recording',
]
CURRENCY = 'usd'
DRAFT_LIFETIME = timedelta(days=30)

# Stripe recurring price interval for each billing-interval period length.
# Quarterly is billed monthly-priced but every 3 months, not "every quarter"
# as a distinct unit - Stripe has no native "quarter" interval.
STRIPE_INTERVAL_BY_MONTHS = {1: ('month', 1), 3: ('month', 3), 12: ('year', 1)}

STRIPE_SUBSCRIPTION_STATUS_MAP = {
    'active': 'active', 'trialing': 'active', 'past_due': 'past_due',
    'canceled': 'canceled', 'unpaid': 'past_due', 'incomplete': 'pending',
    'incomplete_expired': 'canceled',
}


class ConfigurationError(Exception):
    """A configuration selection does not validate against the catalog.

    Carries DRF-shaped field errors (`errors`) so a draft save can surface
    them directly; checkout instead maps this to a single friendly 409, since
    by the time someone is checking out a field-by-field form is no longer
    on screen.
    """

    def __init__(self, errors):
        super().__init__('Enterprise configuration failed validation.')
        self.errors = errors


class CatalogUnavailable(Exception):
    """No active PricingCatalogVersion exists - nothing to price against."""


class DraftNotFound(Exception):
    pass


class DraftAccessDenied(Exception):
    def __init__(self, message='You do not have access to this plan configuration.'):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def get_active_catalog():
    return PricingCatalogVersion.objects.filter(is_active=True).order_by('-version').first()


def serialize_catalog(catalog):
    limits = {category: [] for category in LIMIT_CATEGORIES}
    analytics = []
    credit_packages = []

    for option in catalog.options.filter(is_active=True):
        entry = {'value': option.value, 'label': option.label, 'price_cents': option.price_cents}
        if option.category in limits:
            limits[option.category].append(entry)
        elif option.category == 'analytics':
            analytics.append(entry)
        elif option.category == 'credit_package':
            credit_packages.append(entry)

    features = [
        {
            'key': feature.key, 'label': feature.label,
            'description': feature.description, 'price_cents': feature.price_cents,
        }
        for feature in catalog.features.filter(is_active=True)
    ]
    billing_intervals = [
        {
            'value': interval.code, 'label': interval.label,
            'months': interval.months, 'discount_percent': interval.discount_percent,
        }
        for interval in catalog.billing_intervals.all()
    ]
    base_fee = getattr(catalog, 'base_fee', None)

    return {
        'catalog_version': catalog.version,
        'currency': CURRENCY,
        'base_fee_cents': base_fee.amount_cents if base_fee else 0,
        'limits': limits,
        'features': features,
        'analytics': analytics,
        'credit_packages': credit_packages,
        'billing_intervals': billing_intervals,
    }


def _is_plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def price_configuration(configuration, catalog):
    """Validate `configuration` against `catalog` and price it.

    Returns `(normalized_configuration, pricing, line_items)`. Raises
    `ConfigurationError` with field-shaped errors for anything missing,
    malformed, or pointing at an option that isn't active in this catalog
    version - never clamps to a nearby valid value.
    """
    if not isinstance(configuration, dict):
        raise ConfigurationError({'configuration': ['This field must be an object.']})

    errors = {}

    if configuration.get('type') != 'plan':
        errors['type'] = ['Must be "plan".']
    if configuration.get('code') != 'enterprise':
        errors['code'] = ['Must be "enterprise".']

    intervals_by_code = {interval.code: interval for interval in catalog.billing_intervals.all()}
    billing_interval = configuration.get('billing_interval')
    interval = intervals_by_code.get(billing_interval)
    if interval is None:
        errors['billing_interval'] = [f'"{billing_interval}" is not a valid billing interval.']

    options_by_key = {
        (option.category, option.value): option
        for option in catalog.options.filter(is_active=True)
    }

    limits_in = configuration.get('limits')
    if not isinstance(limits_in, dict):
        errors['limits'] = ['This field must be an object.']
        limits_in = {}
    limits_out = {}
    matched_limit_options = {}
    limit_errors = {}
    for category in LIMIT_CATEGORIES:
        raw_value = limits_in.get(category)
        if not _is_plain_int(raw_value):
            limit_errors[category] = ['This field is required and must be an integer.']
            continue
        option = options_by_key.get((category, str(raw_value)))
        if option is None:
            limit_errors[category] = [f'"{raw_value}" is not an available option.']
            continue
        limits_out[category] = raw_value
        matched_limit_options[category] = option
    if limit_errors:
        errors['limits'] = limit_errors

    features_in = configuration.get('features')
    if not isinstance(features_in, dict):
        errors['features'] = ['This field must be an object.']
        features_in = {}
    features_by_key = {feature.key: feature for feature in catalog.features.filter(is_active=True)}
    features_out = {}
    matched_features = []
    feature_errors = {}
    for key in FEATURE_KEYS:
        raw_value = features_in.get(key)
        if not isinstance(raw_value, bool):
            feature_errors[key] = ['This field is required and must be true or false.']
            continue
        if raw_value:
            feature = features_by_key.get(key)
            if feature is None:
                feature_errors[key] = ['This feature is not currently available.']
                continue
            matched_features.append(feature)
        features_out[key] = raw_value

    analytics_value = features_in.get('analytics')
    analytics_option = None
    if not isinstance(analytics_value, str):
        feature_errors['analytics'] = ['This field is required.']
    else:
        analytics_option = options_by_key.get(('analytics', analytics_value))
        if analytics_option is None:
            feature_errors['analytics'] = [f'"{analytics_value}" is not an available analytics tier.']
        else:
            features_out['analytics'] = analytics_value
    if feature_errors:
        errors['features'] = feature_errors

    credits_in = configuration.get('credits')
    if not isinstance(credits_in, dict):
        errors['credits'] = ['This field must be an object.']
        credits_in = {}
    credits_out = {}
    credit_option = None
    credit_errors = {}
    raw_credits = credits_in.get('monthly_messages')
    if not _is_plain_int(raw_credits):
        credit_errors['monthly_messages'] = ['This field is required and must be an integer.']
    else:
        credit_option = options_by_key.get(('credit_package', str(raw_credits)))
        if credit_option is None:
            credit_errors['monthly_messages'] = [f'"{raw_credits}" is not an available credit package.']
        else:
            credits_out['monthly_messages'] = raw_credits
    if credit_errors:
        errors['credits'] = credit_errors

    if errors:
        raise ConfigurationError(errors)

    base_fee = getattr(catalog, 'base_fee', None)
    base_fee_cents = base_fee.amount_cents if base_fee else 0

    line_items = [{'description': 'Enterprise base fee', 'amount_cents': base_fee_cents, 'category': 'base_fee'}]
    for category in LIMIT_CATEGORIES:
        option = matched_limit_options[category]
        line_items.append({'description': option.label, 'amount_cents': option.price_cents, 'category': category})
    for feature in matched_features:
        line_items.append({
            'description': feature.label, 'amount_cents': feature.price_cents,
            'category': f'feature:{feature.key}',
        })
    if analytics_option is not None and analytics_value != 'none':
        line_items.append({
            'description': f'{analytics_option.label} analytics',
            'amount_cents': analytics_option.price_cents, 'category': 'analytics',
        })
    line_items.append({
        'description': credit_option.label, 'amount_cents': credit_option.price_cents,
        'category': 'credit_package',
    })

    monthly_subtotal_cents = sum(item['amount_cents'] for item in line_items)
    period_months = interval.months
    period_subtotal_cents = monthly_subtotal_cents * period_months
    discount_cents = round(period_subtotal_cents * interval.discount_percent / 100)
    period_total_cents = period_subtotal_cents - discount_cents

    normalized_configuration = {
        'type': 'plan',
        'code': 'enterprise',
        'billing_interval': interval.code,
        'limits': limits_out,
        'features': features_out,
        'credits': credits_out,
    }
    pricing = {
        'billing_interval': interval.code,
        'period_months': period_months,
        'monthly_subtotal_cents': monthly_subtotal_cents,
        'period_subtotal_cents': period_subtotal_cents,
        'discount_percent': interval.discount_percent,
        'discount_cents': discount_cents,
        'period_total_cents': period_total_cents,
    }
    return normalized_configuration, pricing, line_items


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

def generate_draft_id():
    return f'ent_draft_{secrets.token_hex(6)}'


def ensure_session_key(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def draft_belongs_to_request(draft, request):
    if draft.account_id is not None:
        user = getattr(request, 'user', None)
        return bool(user and user.is_authenticated and draft.account_id == user.id)
    session_key = request.session.session_key
    return bool(session_key and draft.session_key == session_key)


@transaction.atomic
def save_draft(*, draft_id, configuration, request):
    """Create or update a draft, pricing `configuration` against the active
    catalog. Raises `ConfigurationError`, `DraftNotFound` or
    `DraftAccessDenied` - callers translate those into the right response."""
    catalog = get_active_catalog()
    if catalog is None:
        raise CatalogUnavailable()

    normalized_configuration, pricing, _line_items = price_configuration(configuration, catalog)

    draft = None
    if draft_id:
        draft = EnterprisePlanDraft.objects.filter(draft_id=draft_id).first()
        if draft is None:
            raise DraftNotFound()
        if not draft_belongs_to_request(draft, request):
            raise DraftAccessDenied()

    user = getattr(request, 'user', None)
    is_authenticated = bool(user and user.is_authenticated)
    created = draft is None

    if draft is None:
        draft = EnterprisePlanDraft(draft_id=draft_id or generate_draft_id())
        if is_authenticated:
            draft.account = user
        else:
            draft.session_key = ensure_session_key(request)

    draft.configuration = normalized_configuration
    draft.catalog_version = catalog
    draft.currency = CURRENCY
    draft.calculated_subtotal_cents = pricing['period_subtotal_cents']
    draft.discount_cents = pricing['discount_cents']
    draft.calculated_total_cents = pricing['period_total_cents']
    draft.expires_at = timezone.now() + DRAFT_LIFETIME
    draft.save()
    return draft, created


def get_owned_draft(draft_id, request):
    draft = EnterprisePlanDraft.objects.filter(draft_id=draft_id).first()
    if draft is None or not draft_belongs_to_request(draft, request):
        return None
    return draft


def attach_draft(draft, user):
    if draft.account_id is not None and draft.account_id != user.id:
        raise DraftAccessDenied('This plan configuration is already linked to another account.')
    if draft.account_id is None:
        draft.account = user
        draft.save(update_fields=['account', 'updated_at'])
    return draft


def serialize_draft_response(draft):
    normalized_configuration, pricing, _line_items = price_configuration(
        draft.configuration, draft.catalog_version
    )
    return {
        'draft_id': draft.draft_id,
        'catalog_version': draft.catalog_version.version,
        'currency': draft.currency,
        'configuration': normalized_configuration,
        'pricing': pricing,
        'status': draft.status,
        'expires_at': draft.expires_at,
    }


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def _idempotency_key(*parts, bucket_seconds):
    import hashlib
    import time
    bucket = int(time.time() // bucket_seconds)
    raw = ':'.join(str(part) for part in (*parts, bucket))
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_contract_number():
    """The next `ENT-<year>-<6 digits>` number for this year's prefix.

    Derived from the highest existing suffix, not `count()` of matching rows -
    a row count assumes numbers are always contiguous from 1, which breaks as
    soon as one is missing (a deleted test contract, a cleaned-up record): the
    count no longer matches the highest number in use, so it can regenerate an
    already-taken number. Worse, `_create_contract`'s retry-on-IntegrityError
    loop calls this again unchanged on every attempt, so a stale count means
    every retry collides on the identical number and the whole loop is wasted.
    Zero-padded to a fixed width, so ordering by the string sorts the same as
    ordering by the number - no need to pull every row and parse it in Python.
    """
    year = timezone.now().year
    prefix = f'ENT-{year}-'
    last_number = (
        EnterpriseContract.objects
        .filter(contract_number__startswith=prefix)
        .order_by('-contract_number')
        .values_list('contract_number', flat=True)
        .first()
    )
    next_suffix = int(last_number[len(prefix):]) + 1 if last_number else 1
    return f'{prefix}{next_suffix:06d}'


@transaction.atomic
def _create_contract(*, draft, user, catalog, configuration, pricing, line_items):
    pricing_snapshot = {
        'currency': CURRENCY,
        'billing_interval': pricing['billing_interval'],
        'period_months': pricing['period_months'],
        'monthly_subtotal_cents': pricing['monthly_subtotal_cents'],
        'subtotal_cents': pricing['period_subtotal_cents'],
        'discount_percent': pricing['discount_percent'],
        'discount_cents': pricing['discount_cents'],
        'total_cents': pricing['period_total_cents'],
        'line_items': line_items,
    }
    for _attempt in range(5):
        try:
            with transaction.atomic():
                return EnterpriseContract.objects.create(
                    account=user,
                    contract_number=generate_contract_number(),
                    draft=draft,
                    configuration_snapshot=configuration,
                    pricing_snapshot=pricing_snapshot,
                    catalog_version=catalog,
                    billing_interval=pricing['billing_interval'],
                    status='pending',
                )
        except IntegrityError:
            continue
    raise RuntimeError('Could not allocate a unique Enterprise contract number.')


def _create_stripe_checkout_session(*, user, contract, draft_id, customer_id, pricing, line_items,
                                     success_url, cancel_url):
    interval_unit, interval_count = STRIPE_INTERVAL_BY_MONTHS[pricing['period_months']]
    stripe_line_items = [
        {
            'price_data': {
                'currency': CURRENCY,
                'unit_amount': item['amount_cents'],
                'recurring': {'interval': interval_unit, 'interval_count': interval_count},
                'product_data': {'name': item['description']},
            },
            'quantity': 1,
        }
        for item in line_items
    ]

    session_kwargs = dict(
        mode='subscription',
        customer=customer_id,
        line_items=stripe_line_items,
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user.pk),
        metadata={
            'user_id': str(user.pk),
            'kind': 'enterprise_contract',
            'contract_id': str(contract.pk),
            'contract_number': contract.contract_number,
            'draft_id': contract.draft.draft_id,
        },
        # Keyed on the draft, not `contract.pk` - `_create_contract` mints a
        # fresh contract (and so a fresh pk) on every call, including a
        # genuine retry of this same checkout, so a pk-based key could never
        # actually land a retry on the earlier Stripe request. `draft_id` is
        # the stable, pre-existing identity of "this configuration, being
        # checked out" - same reasoning as `create_plan_checkout_session`
        # keying on `(user.pk, plan.code)` in billing/services.py.
        idempotency_key=_idempotency_key('enterprise-checkout', draft_id, bucket_seconds=300),
    )

    if pricing['discount_percent'] > 0:
        coupon = stripe.Coupon.create(
            percent_off=pricing['discount_percent'],
            duration='forever',
            name=f"{pricing['billing_interval'].title()} plan discount",
            idempotency_key=_idempotency_key('enterprise-coupon', draft_id, bucket_seconds=300),
        )
        session_kwargs['discounts'] = [{'coupon': coupon['id']}]

    return stripe.checkout.Session.create(**session_kwargs)


def checkout_draft(draft, user, *, success_url, cancel_url):
    """Revalidate `draft` against the CURRENT active catalog, freeze it into
    a new EnterpriseContract, and open a Stripe Checkout Session for it.

    Raises `ConfigurationError` if anything the draft selected is no longer
    active - the view maps that to a 409 telling the caller to reconfigure,
    rather than silently billing whatever still happens to validate.

    `success_url`/`cancel_url` come from the caller, already checked by
    `DraftCheckoutRequestSerializer` against the same allowed origins as the
    rest of billing's checkout endpoints - never taken on trust here.
    """
    catalog = get_active_catalog()
    if catalog is None:
        raise CatalogUnavailable()

    normalized_configuration, pricing, line_items = price_configuration(draft.configuration, catalog)

    contract = _create_contract(
        draft=draft, user=user, catalog=catalog,
        configuration=normalized_configuration, pricing=pricing, line_items=line_items,
    )
    draft.status = 'converted'
    draft.save(update_fields=['status', 'updated_at'])

    customer_id = billing_services.get_or_create_stripe_customer(user)
    session = _create_stripe_checkout_session(
        user=user, contract=contract, draft_id=draft.draft_id, customer_id=customer_id,
        pricing=pricing, line_items=line_items,
        success_url=success_url, cancel_url=cancel_url,
    )

    EnterpriseStripeBilling.objects.create(
        contract=contract,
        stripe_customer_id=customer_id,
        stripe_checkout_session_id=session['id'],
        status=session['status'] or 'open',
    )

    invoice_payload = {
        'currency': CURRENCY,
        'billing_interval': pricing['billing_interval'],
        'line_items': [
            {'description': item['description'], 'amount_cents': item['amount_cents']}
            for item in line_items
        ],
        'subtotal_cents': pricing['period_subtotal_cents'],
        'discount_cents': pricing['discount_cents'],
        'total_cents': pricing['period_total_cents'],
    }
    return contract, session, invoice_payload


def get_current_contract(user):
    return EnterpriseContract.objects.filter(account=user).order_by('-created_at').first()


# ---------------------------------------------------------------------------
# Stripe webhook handlers - called from billing.webhooks.StripeWebhook.
# ---------------------------------------------------------------------------

def is_enterprise_checkout(session):
    return (session.get('metadata') or {}).get('kind') == 'enterprise_contract'


def is_enterprise_subscription(stripe_subscription_id):
    if not stripe_subscription_id:
        return False
    return EnterpriseStripeBilling.objects.filter(stripe_subscription_id=stripe_subscription_id).exists()


def _sync_entitlements(contract):
    configuration = contract.configuration_snapshot
    rows = []
    for category in LIMIT_CATEGORIES:
        rows.append(EnterpriseEntitlement(
            contract=contract, feature_key=category, enabled=True,
            limit_value=configuration['limits'][category],
            limit_type='gb' if category == 'storage_gb' else 'count',
        ))
    for key in FEATURE_KEYS:
        # .get, not [key]: a contract frozen before a key existed in
        # FEATURE_KEYS has no opinion on it, and that reads as "not bought"
        # rather than a KeyError the next time this contract is touched.
        rows.append(EnterpriseEntitlement(
            contract=contract, feature_key=key,
            enabled=bool(configuration['features'].get(key)),
        ))
    analytics_tier = configuration['features'].get('analytics', 'none')
    rows.append(EnterpriseEntitlement(
        contract=contract, feature_key='analytics',
        enabled=(analytics_tier != 'none'), limit_type=analytics_tier,
    ))
    rows.append(EnterpriseEntitlement(
        contract=contract, feature_key='monthly_messages', enabled=True,
        limit_value=configuration['credits']['monthly_messages'], limit_type='messages_per_month',
    ))

    existing = {row.feature_key: row for row in EnterpriseEntitlement.objects.filter(contract=contract)}
    to_create = []
    for row in rows:
        existing_row = existing.pop(row.feature_key, None)
        if existing_row is None:
            to_create.append(row)
        else:
            existing_row.enabled = row.enabled
            existing_row.limit_value = row.limit_value
            existing_row.limit_type = row.limit_type
            existing_row.save(update_fields=['enabled', 'limit_value', 'limit_type'])
    if to_create:
        EnterpriseEntitlement.objects.bulk_create(to_create)


def _feature_labels_for_contract(contract):
    """Marketing-style bullet points for `Plan.features` - display only, not
    read by any enforcement code, same as the regular Plan catalog's."""
    configuration = contract.configuration_snapshot
    catalog = contract.catalog_version
    features_by_key = {feature.key: feature for feature in catalog.features.all()}
    options_by_key = {(option.category, option.value): option for option in catalog.options.all()}

    labels = [
        features_by_key[key].label
        for key in FEATURE_KEYS
        if configuration['features'].get(key) and key in features_by_key
    ]
    analytics_tier = configuration['features'].get('analytics', 'none')
    if analytics_tier != 'none':
        option = options_by_key.get(('analytics', analytics_tier))
        if option is not None:
            labels.append(f'{option.label} analytics')
    return labels


def _sync_account_plan(contract, *, current_period_end=None):
    """Point the account's billing Subscription at a Plan mirroring this
    contract's limits and features.

    `billing.limits.check_can_create_group` and the billing summary's
    `current_plan_code` both read `Subscription.plan` - without this, an
    Enterprise account would keep showing (and being capped by) whatever
    plan it was on before checkout, even though the contract and its
    entitlements say otherwise.
    """
    limits = contract.configuration_snapshot['limits']
    # .get, not [key]: see the same comment in _sync_entitlements.
    features = contract.configuration_snapshot['features']
    voice_calls = bool(features.get('voice_calls'))
    video_calls = bool(features.get('video_calls'))
    call_recording = bool(features.get('call_recording'))

    plan, _ = Plan.objects.update_or_create(
        code=contract.contract_number.lower(),
        defaults={
            'name': f'Enterprise ({contract.contract_number})',
            'monthly_price_cents': None,
            'max_groups': limits['max_groups'],
            'max_members_per_group': limits['max_members_per_group'],
            'max_admins_per_group': limits['max_admins_per_group'],
            'storage_gb': limits['storage_gb'],
            # Calling is priced per feature for a custom plan, unlike the four
            # platform plans, where it ships as standard - see the comment on
            # `Plan.has_calls`. A video call is still a call: buying
            # `video_calls` alone is enough to turn calling on at all, so a
            # buyer is never left with video access and no way to place a
            # plain audio call.
            'has_calls': voice_calls or video_calls,
            'has_video_calls': video_calls,
            # Not sold as its own line item yet, so every contract gets it -
            # the same placeholder the catalog had before calling existed at
            # all. Worth its own FEATURE_KEYS entry once screen-sharing during
            # a call is something Enterprise buyers are actually charged for.
            'has_screen_share': True,
            'has_call_recording': call_recording,
            # A flat 90 days when bought. Not yet its own priced tier the way
            # storage_gb is - the whole retention PERIOD is currently included
            # in the flat `call_recording` fee, not sold in slices.
            'call_recording_retention_days': 90 if call_recording else None,
            'max_call_participants': None,
            'features': _feature_labels_for_contract(contract),
            'is_custom_pricing': True,
            'active': True,
        },
    )

    subscription = billing_services.get_or_create_subscription(contract.account)
    subscription.plan = plan
    subscription.status = Subscription.Status.ACTIVE
    subscription.current_period_end = current_period_end
    subscription.cancel_at_period_end = False
    subscription.save(
        update_fields=['plan', 'status', 'current_period_end', 'cancel_at_period_end', 'updated_at']
    )
    return plan


def current_balance(contract):
    last_entry = CreditLedgerEntry.objects.filter(contract=contract).order_by('-created_at', '-id').first()
    return last_entry.balance_after if last_entry else 0


def _allocate_initial_credits(contract, *, reference_id):
    if CreditLedgerEntry.objects.filter(contract=contract, transaction_type='allocation').exists():
        return
    credits = contract.configuration_snapshot['credits']['monthly_messages']
    CreditLedgerEntry.objects.create(
        contract=contract, transaction_type='allocation', amount=credits, balance_after=credits,
        reference_type='checkout', reference_id=str(reference_id),
    )


def handle_checkout_completed(session):
    metadata = session.get('metadata') or {}
    contract_id = metadata.get('contract_id')
    if not contract_id:
        logger.error('Enterprise checkout.session.completed missing contract_id: %s', session.get('id'))
        return

    contract = EnterpriseContract.objects.filter(pk=contract_id).select_related('draft').first()
    if contract is None:
        logger.error('Enterprise checkout.session.completed for unknown contract %s', contract_id)
        return

    stripe_subscription_id = session.get('subscription') or ''
    stripe_invoice_id = ''
    current_period_end = None
    if stripe_subscription_id:
        try:
            # .retrieve() returns a real Stripe SDK object, not a dict - same
            # gotcha billing/webhooks.py converts around (see its module
            # docstring): .get(...) on one raises AttributeError rather than
            # falling through to dict behaviour, so it must be converted
            # before anything downstream treats it as one.
            stripe_subscription = stripe.Subscription.retrieve(stripe_subscription_id).to_dict()
            latest_invoice = stripe_subscription.get('latest_invoice')
            stripe_invoice_id = latest_invoice if isinstance(latest_invoice, str) else (
                getattr(latest_invoice, 'id', '') or ''
            )
            # Recent Stripe API versions moved current_period_end off the
            # Subscription itself and onto each SubscriptionItem - the
            # top-level field is None on those versions, so fall back to the
            # first item's, matching the one price this checkout creates.
            raw_period_end = stripe_subscription.get('current_period_end')
            if raw_period_end is None:
                items = (stripe_subscription.get('items') or {}).get('data') or []
                if items:
                    raw_period_end = items[0].get('current_period_end')
            current_period_end = billing_services.stripe_timestamp_to_datetime(raw_period_end)
        except stripe.StripeError:
            logger.exception('Failed retrieving Stripe subscription %s', stripe_subscription_id)

    EnterpriseStripeBilling.objects.update_or_create(
        contract=contract,
        defaults={
            'stripe_customer_id': session.get('customer') or '',
            'stripe_subscription_id': stripe_subscription_id,
            'stripe_invoice_id': stripe_invoice_id,
            'stripe_checkout_session_id': session.get('id') or '',
            'status': 'active',
        },
    )

    contract.status = 'active'
    contract.started_at = timezone.now()
    contract.save(update_fields=['status', 'started_at'])

    _sync_entitlements(contract)
    _sync_account_plan(contract, current_period_end=current_period_end)
    _allocate_initial_credits(contract, reference_id=session.get('id') or contract.contract_number)


def handle_invoice_paid(invoice):
    subscription_id = invoice.get('subscription')
    billing_row = EnterpriseStripeBilling.objects.filter(
        stripe_subscription_id=subscription_id
    ).select_related('contract').first() if subscription_id else None
    if billing_row is None:
        return

    contract = billing_row.contract
    billing_row.stripe_invoice_id = invoice.get('id') or billing_row.stripe_invoice_id
    billing_row.status = 'active'
    billing_row.save(update_fields=['stripe_invoice_id', 'status'])

    if contract.status != 'active':
        contract.status = 'active'
        contract.save(update_fields=['status'])

    # The invoice behind checkout.session.completed itself ("subscription_create")
    # already had its credits allocated there - only a renewal replenishes.
    # No-rollover policy, explicit: each period's balance is REPLACED by the
    # package amount, never added on top of what is left.
    if invoice.get('billing_reason') != 'subscription_cycle':
        return

    package_credits = contract.configuration_snapshot['credits']['monthly_messages']
    balance_before = current_balance(contract)
    CreditLedgerEntry.objects.create(
        contract=contract, transaction_type='allocation',
        amount=package_credits - balance_before, balance_after=package_credits,
        reference_type='invoice', reference_id=str(invoice.get('id') or ''),
    )


def handle_invoice_payment_failed(invoice):
    subscription_id = invoice.get('subscription')
    billing_row = EnterpriseStripeBilling.objects.filter(
        stripe_subscription_id=subscription_id
    ).select_related('contract').first() if subscription_id else None
    if billing_row is None:
        return

    billing_row.status = 'past_due'
    billing_row.save(update_fields=['status'])
    contract = billing_row.contract
    contract.status = 'past_due'
    contract.save(update_fields=['status'])


def handle_subscription_updated(stripe_subscription):
    billing_row = EnterpriseStripeBilling.objects.filter(
        stripe_subscription_id=stripe_subscription.get('id')
    ).select_related('contract').first()
    if billing_row is None:
        return

    stripe_status = stripe_subscription.get('status') or ''
    billing_row.status = stripe_status
    billing_row.save(update_fields=['status'])

    contract = billing_row.contract
    mapped_status = STRIPE_SUBSCRIPTION_STATUS_MAP.get(stripe_status, contract.status)
    if contract.status != mapped_status:
        contract.status = mapped_status
        contract.save(update_fields=['status'])


def handle_subscription_deleted(stripe_subscription):
    billing_row = EnterpriseStripeBilling.objects.filter(
        stripe_subscription_id=stripe_subscription.get('id')
    ).select_related('contract').first()
    if billing_row is None:
        return

    billing_row.status = 'canceled'
    billing_row.save(update_fields=['status'])

    contract = billing_row.contract
    contract.status = 'canceled'
    contract.ended_at = timezone.now()
    contract.save(update_fields=['status', 'ended_at'])
    # Immediate, matching billing.services.downgrade_to_free's own
    # end-of-grace behaviour rather than waiting out the period.
    EnterpriseEntitlement.objects.filter(contract=contract).update(enabled=False)

    # Only if the account is still on the Plan this contract installed -
    # it may already have moved on to a different subscription since.
    subscription = billing_services.get_or_create_subscription(contract.account)
    if subscription.plan.code == contract.contract_number.lower():
        billing_services.downgrade_to_free(contract.account)


def handle_charge_refunded(charge):
    invoice_id = charge.get('invoice')
    billing_row = None
    if invoice_id:
        billing_row = EnterpriseStripeBilling.objects.filter(
            stripe_invoice_id=invoice_id
        ).select_related('contract').first()
    if billing_row is None:
        customer_id = charge.get('customer')
        if customer_id:
            billing_row = EnterpriseStripeBilling.objects.filter(
                stripe_customer_id=customer_id
            ).select_related('contract').order_by('-id').first()
    if billing_row is None:
        return

    contract = billing_row.contract
    refunded_cents = charge.get('amount_refunded') or 0
    period_total_cents = (contract.pricing_snapshot or {}).get('total_cents') or 0
    package_credits = contract.configuration_snapshot['credits']['monthly_messages']

    # A refund is a dollar figure; the ledger tracks message credits. Claw
    # back the same share of this period's credit allocation as the refund
    # is of this period's total charge - the closest even split available
    # without a separate cents-denominated ledger.
    clawback = round(package_credits * (refunded_cents / period_total_cents)) if period_total_cents else 0

    balance_before = current_balance(contract)
    new_balance = max(0, balance_before - clawback)
    CreditLedgerEntry.objects.create(
        contract=contract, transaction_type='refund', amount=new_balance - balance_before,
        balance_after=new_balance, reference_type='stripe_refund', reference_id=str(charge.get('id') or ''),
    )
