"""Receives events Stripe pushes about payments and subscriptions.

Like sms/webhooks.py, this is an unauthenticated public endpoint - the caller
is Stripe's servers, not a person with a token, and the only proof of that is
a signature over the raw body. Unlike the SMS provider, Stripe's own SDK does
the signature verification (`stripe.Webhook.construct_event`), so there is no
hand-rolled crypto here.

STATUS CODES ARE THE CONTRACT
-----------------------------
Stripe reads any non-2xx as "that did not arrive" and retries for hours:

  200  every outcome we understand and will not change our minds about -
       duplicate event, or an event type we do not act on.
  400  a bad signature, or an unparseable payload. Never retried.
  5xx  we failed to process a well-formed, known event. The de-duplication
       row is removed first, so the retry can reprocess it rather than being
       waved through as a duplicate.

Plans and credit packages can also be mirrored from Stripe Product/Price
create-update events by embedding metadata.data JSON. That allows the Stripe
dashboard itself to be the source of truth when desired.
"""
import json
import logging

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import services
from billing.models import CreditPackage, Plan, StripeWebhookEvent, Subscription
from utils.mail import EmailError, send_templated_email

logger = logging.getLogger(__name__)


class StripeWebhook(APIView):
    """Receives everything Stripe has to tell us about an event."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # Stripe expects a bare status code and reads no body, so this answers in
    # plain JSON rather than the project's `{code, message, data}` envelope.
    renderer_classes = [JSONRenderer]

    def post(self, request):
        try:
            event = stripe.Webhook.construct_event(
                request.body,
                request.META.get('HTTP_STRIPE_SIGNATURE', ''),
                settings.STRIPE_WEBHOOK_SECRET,
            )
        except ValueError:
            logger.warning('Unparseable Stripe webhook body')
            return Response({'ok': False}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.SignatureVerificationError:
            logger.warning('Stripe webhook signature verification failed')
            return Response({'ok': False}, status=status.HTTP_400_BAD_REQUEST)

        event_id = event['id']
        event_type = event['type']

        # De-duplicate FIRST, before anything with a side effect. A retried
        # event that got as far as granting access must not grant it twice.
        _, created = StripeWebhookEvent.objects.get_or_create(
            event_id=event_id, defaults={'event_type': event_type}
        )
        if not created:
            return Response({'ok': True, 'duplicate': True}, status=status.HTTP_200_OK)

        try:
            # `event['data']['object']` is a Stripe SDK object (Session,
            # Subscription, ...) coming from the real `construct_event`, not a
            # dict - newer stripe-python versions raise AttributeError on
            # `.get(...)` against one rather than falling through to dict
            # behaviour. `_handle` and its helpers are all written against a
            # plain dict, so convert once here rather than bracket-accessing
            # everything downstream. Tests that stub `construct_event` hand
            # back an already-plain dict, which has no `.to_dict()` - kept as
            # is in that case.
            data_object = event['data']['object']
            if hasattr(data_object, 'to_dict'):
                data_object = data_object.to_dict()
            self._handle(event_type, data_object)
        except Exception:
            # Delete the dedupe row so Stripe's retry can actually reprocess
            # this event rather than being waved through as a duplicate.
            StripeWebhookEvent.objects.filter(event_id=event_id).delete()
            logger.exception('Failed handling Stripe event %s; asking for a retry',
                             event_id)
            return Response(
                {'ok': False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({'ok': True}, status=status.HTTP_200_OK)

    def _handle(self, event_type, data_object):
        if event_type == 'checkout.session.completed':
            self._handle_checkout_completed(data_object)
        elif event_type == 'checkout.session.async_payment_succeeded':
            self._handle_async_payment_succeeded(data_object)
        elif event_type in ('customer.subscription.updated', 'customer.subscription.created'):
            self._handle_subscription_updated(data_object)
        elif event_type == 'customer.subscription.deleted':
            self._handle_subscription_deleted(data_object)
        elif event_type == 'invoice.paid':
            self._handle_invoice_paid(data_object)
        elif event_type == 'invoice.payment_failed':
            self._handle_invoice_payment_failed(data_object)
        elif event_type == 'charge.refunded':
            self._handle_charge_refunded(data_object)
        elif event_type == 'product.deleted':
            self._handle_product_deactivated(data_object, source_event='product.deleted')
        elif event_type == 'product.updated' and self._is_product_archived(data_object):
            self._handle_product_deactivated(data_object, source_event='product.updated')
        elif event_type in ('product.created', 'product.updated', 'price.created', 'price.updated'):
            self._handle_catalog_changed(event_type, data_object)
        else:
            logger.info('Received Stripe event %s (unhandled)', event_type)

    def _is_product_archived(self, product):
        return product.get('active') is False

    def _handle_product_deactivated(self, product, *, source_event):
        product_id = product.get('id')
        if not product_id:
            logger.warning('Stripe %s missing product id; skipping', source_event)
            return

        matched_plans = 0
        matched_packages = 0

        plans = Plan.objects.filter(active=True).exclude(stripe_price_id='')
        for plan in plans:
            if plan.stripe_product_id == product_id or self._price_belongs_to_product(
                plan.stripe_price_id, product_id
            ):
                plan.active = False
                plan.save(update_fields=['active', 'updated_at'])
                matched_plans += 1

        packages = CreditPackage.objects.filter(active=True).exclude(stripe_price_id='')
        for package in packages:
            if package.stripe_product_id == product_id or self._price_belongs_to_product(
                package.stripe_price_id, product_id
            ):
                package.active = False
                package.save(update_fields=['active', 'updated_at'])
                matched_packages += 1

        if matched_plans == 0 and matched_packages == 0:
            logger.info('Stripe %s %s had no matching plan/credit rows', source_event, product_id)
            return

        logger.info(
            'Stripe %s %s deactivated %s plan(s) and %s credit package(s)',
            source_event,
            product_id,
            matched_plans,
            matched_packages,
        )

    def _handle_catalog_changed(self, event_type, stripe_object):
        if event_type.startswith('product.'):
            product = stripe_object
            price = self._latest_price_for_product(product.get('id'))
        else:
            price = stripe_object
            product = self._safe_retrieve_product(price.get('product'))

        if product is None:
            logger.warning('Stripe %s without a retrievable product; skipping', event_type)
            return

        if price is None and event_type == 'product.created':
            payload = self._catalog_payload(product.get('metadata') or {}, {})
            if payload and str(payload.get('type') or '').strip().lower() == 'plan':
                self._upsert_plan_from_product_only(product, payload)
                return

        if price is None:
            logger.warning('Stripe %s for product %s has no active price yet; skipping',
                           event_type, product.get('id'))
            return

        payload = self._catalog_payload(
            product.get('metadata') or {},
            price.get('metadata') or {},
        )
        if not payload:
            logger.info('Stripe catalog event %s missing metadata.data payload; skipping', event_type)
            return

        item_type = str(payload.get('type') or '').strip().lower()
        if item_type == 'plan':
            self._upsert_plan_from_catalog(product, price, payload)
        elif item_type == 'credit':
            self._upsert_credit_from_catalog(product, price, payload)
        else:
            logger.warning('Stripe catalog metadata has unknown type %r; skipping', item_type)

    def _catalog_payload(self, product_metadata, price_metadata):
        price_payload = self._parse_metadata_data(price_metadata.get('data'))
        if price_payload:
            return price_payload

        product_payload = self._parse_metadata_data(product_metadata.get('data'))
        if product_payload:
            return product_payload

        # Backward-compatible fallback for older metadata keys.
        plan_code = price_metadata.get('plan_code') or product_metadata.get('plan_code')
        if plan_code:
            return {'type': 'plan', 'code': plan_code}

        package_id = (
            price_metadata.get('credit_package_id')
            or product_metadata.get('credit_package_id')
        )
        if package_id:
            return {'type': 'credit', 'id': package_id}
        return None

    def _parse_metadata_data(self, raw_value):
        if isinstance(raw_value, dict):
            return raw_value
        if not raw_value or not isinstance(raw_value, str):
            return None
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            logger.warning('Ignoring Stripe metadata.data with invalid JSON: %r', raw_value)
            return None
        if not isinstance(parsed, dict):
            logger.warning('Ignoring Stripe metadata.data that is not an object')
            return None
        return parsed

    def _safe_retrieve_product(self, product_id):
        if not product_id:
            return None
        try:
            return self._to_dict(stripe.Product.retrieve(product_id))
        except stripe.StripeError:
            logger.exception('Failed retrieving Stripe product %s', product_id)
            return None

    def _latest_price_for_product(self, product_id):
        if not product_id:
            return None
        try:
            for price in stripe.Price.list(product=product_id, active=True, limit=100).auto_paging_iter():
                return self._to_dict(price)
            return None
        except stripe.StripeError:
            logger.exception('Failed listing Stripe prices for product %s', product_id)
            return None

    def _safe_retrieve_price(self, price_id):
        if not price_id:
            return None
        try:
            return self._to_dict(stripe.Price.retrieve(price_id))
        except stripe.StripeError:
            return None

    def _price_belongs_to_product(self, price_id, product_id):
        price = self._safe_retrieve_price(price_id)
        if not price:
            return False
        price_product = price.get('product')
        if isinstance(price_product, dict):
            price_product = price_product.get('id')
        return str(price_product or '') == str(product_id or '')

    def _upsert_plan_from_catalog(self, product, price, payload):
        code = str(payload.get('code') or '').strip().lower()
        product_id = str(product.get('id') or '')
        base_code = code or self._safe_plan_code_from_name(product.get('name') or 'plan')

        price_cents = self._coerce_int(price.get('unit_amount'))
        if price_cents is None:
            logger.warning('Plan payload for code %s has no unit_amount; skipping', base_code)
            return

        defaults = {
            'name': str(product.get('name') or base_code.title())[:40],
            'monthly_price_cents': price_cents,
            'stripe_product_id': product_id,
            'stripe_price_id': str(price.get('id') or ''),
            'max_groups': self._coerce_int(payload.get('max_groups')),
            'max_members_per_group': self._coerce_int(payload.get('max_members_per_group')),
            'max_admins_per_group': self._coerce_int(payload.get('max_admins_per_group')),
            'storage_gb': self._coerce_int(payload.get('storage_gb')),
            **self._call_defaults(payload),
            'has_whatsapp': self._coerce_bool(payload.get('has_whatsapp')),
            'has_telegram': self._coerce_bool(payload.get('has_telegram')),
            'features': payload.get('features') if isinstance(payload.get('features'), list) else [],
            'is_custom_pricing': self._coerce_bool(payload.get('is_custom_pricing')),
            'active': self._coerce_bool(product.get('active'), default=True),
        }

        plan, created = self._upsert_plan_record(
            product_id=product_id,
            base_code=base_code,
            defaults=defaults,
            append_only=False,
        )
        recurring = (price.get('recurring') or {}).get('interval') or 'one_time'
        price_display = f'${price_cents / 100:.2f}/{"mo" if recurring == "month" else recurring}'
        logger.info('Stripe catalog %s plan %s from product=%s price=%s (%s)',
                    'created' if created else 'updated',
                    plan.code,
                    product.get('id'),
                    price.get('id'),
                    price_display)

    def _upsert_plan_from_product_only(self, product, payload):
        code = str(payload.get('code') or '').strip().lower()
        product_id = str(product.get('id') or '')
        base_code = code or self._safe_plan_code_from_name(product.get('name') or 'plan')

        defaults = {
            'name': str(product.get('name') or base_code.title())[:40],
            'monthly_price_cents': self._coerce_int(payload.get('monthly_price_cents')),
            'stripe_product_id': product_id,
            'max_groups': self._coerce_int(payload.get('max_groups')),
            'max_members_per_group': self._coerce_int(payload.get('max_members_per_group')),
            'max_admins_per_group': self._coerce_int(payload.get('max_admins_per_group')),
            'storage_gb': self._coerce_int(payload.get('storage_gb')),
            **self._call_defaults(payload),
            'has_whatsapp': self._coerce_bool(payload.get('has_whatsapp')),
            'has_telegram': self._coerce_bool(payload.get('has_telegram')),
            'features': payload.get('features') if isinstance(payload.get('features'), list) else [],
            'is_custom_pricing': self._coerce_bool(payload.get('is_custom_pricing'), default=True),
            'active': self._coerce_bool(product.get('active'), default=True),
        }
        plan, created = self._upsert_plan_record(
            product_id=product_id,
            base_code=base_code,
            defaults=defaults,
            append_only=True,
        )
        logger.info(
            'Stripe catalog %s plan %s from product=%s without price yet',
            'created' if created else 'updated',
            plan.code,
            product.get('id'),
        )

    def _upsert_plan_record(self, *, product_id, base_code, defaults, append_only):
        if product_id:
            plan = Plan.objects.filter(stripe_product_id=product_id).first()
            if plan is not None:
                for field, value in defaults.items():
                    setattr(plan, field, value)
                plan.save(update_fields=[*defaults.keys(), 'updated_at'])
                return plan, False

        if not product_id:
            logger.warning('Plan payload has no Stripe product id; refusing code-based upsert for %s', base_code)
            return Plan.objects.create(code=self._next_unique_plan_code(base_code), **defaults), True

        code = self._next_unique_plan_code(base_code)
        return Plan.objects.create(code=code, **defaults), True

    def _next_unique_plan_code(self, base_code):
        cleaned = ''.join(ch for ch in (base_code or 'plan').lower() if ch.isalnum() or ch in ('-', '_'))
        cleaned = cleaned[:16] or 'plan'
        if not Plan.objects.filter(code=cleaned).exists():
            return cleaned

        suffix = 2
        while True:
            suffix_text = str(suffix)
            candidate = f'{cleaned[:16 - len(suffix_text) - 1]}-{suffix_text}'
            if not Plan.objects.filter(code=candidate).exists():
                return candidate
            suffix += 1

    def _safe_plan_code_from_name(self, name):
        parts = str(name or 'plan').strip().lower().replace("'", '').split()
        if not parts:
            return 'plan'
        return '_'.join(parts)[:16] or 'plan'

    def _upsert_credit_from_catalog(self, product, price, payload):
        product_id = str(product.get('id') or '')
        price_cents = self._coerce_int(price.get('unit_amount'))
        if price_cents is None:
            logger.warning('Credit payload for product %s has no unit_amount; skipping',
                           product.get('id'))
            return

        defaults = {
            'name': str(product.get('name') or 'Credit Package')[:40],
            'price_cents': price_cents,
            'credit_cents': price_cents,
            'stripe_product_id': product_id,
            'stripe_price_id': str(price.get('id') or ''),
            'active': self._coerce_bool(product.get('active'), default=True),
        }

        package_id = self._coerce_int(payload.get('id'))
        if package_id is not None:
            package, created = CreditPackage.objects.update_or_create(pk=package_id, defaults=defaults)
        else:
            package = self._match_credit_package(
                product_id=product_id,
                price_id=defaults['stripe_price_id'],
            )
            if package is None:
                package = CreditPackage.objects.create(**defaults)
                created = True
            else:
                created = False
                for field, value in defaults.items():
                    setattr(package, field, value)
                package.save(update_fields=[*defaults.keys(), 'updated_at'])

        recurring = (price.get('recurring') or {}).get('interval') or 'one_time'
        price_display = f'${price_cents / 100:.2f}/{"mo" if recurring == "month" else recurring}'
        logger.info('Stripe catalog %s credit package %s from product=%s price=%s (%s)',
                    'created' if created else 'updated',
                    package.pk,
                    product.get('id'),
                    price.get('id'),
                    price_display)

    def _match_credit_package(self, *, product_id, price_id):
        if product_id:
            package = CreditPackage.objects.filter(stripe_product_id=product_id).first()
            if package:
                return package

        if price_id:
            package = CreditPackage.objects.filter(stripe_price_id=price_id).first()
            if package:
                return package

        return None

    def _call_defaults(self, payload):
        """The calling fields, read from the same `metadata.data` payload.

        Shared by both upsert paths so a product with only a name and no price
        yet (`_upsert_plan_from_product_only`) gets the same fields a priced
        one does, once its price arrives and this runs again.

        `has_calls` defaults to True - matching `Plan.has_calls`'s own
        db_default - so a product whose metadata has never mentioned calling
        at all is not silently switched off the next time somebody edits its
        price for an unrelated reason. Every other field here defaults the
        way `Plan`'s own column does: off, or no limit.

        `has_screen_share` is not read from the payload at all: it is bundled
        with calling rather than sold separately, the same rule
        `enterprise.services._sync_account_plan` uses for a custom plan -
        whoever has calls gets it, and there is no metadata key that can turn
        it off on its own.
        """
        has_calls = self._coerce_bool(payload.get('has_calls'), default=True)
        return {
            'has_calls': has_calls,
            'has_screen_share': has_calls,
            'max_call_participants': self._coerce_int(payload.get('max_call_participants')),
            'has_call_recording': self._coerce_bool(payload.get('has_call_recording')),
            'call_recording_retention_days': self._coerce_int(
                payload.get('call_recording_retention_days')
            ),
        }

    def _coerce_int(self, value):
        if value in (None, ''):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _coerce_bool(self, value, default=False):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ('1', 'true', 'yes', 'on'):
                return True
            if lowered in ('0', 'false', 'no', 'off'):
                return False
        return bool(value)

    def _to_dict(self, stripe_obj):
        if stripe_obj is None:
            return None
        if isinstance(stripe_obj, dict):
            return stripe_obj
        if hasattr(stripe_obj, 'to_dict_recursive'):
            return stripe_obj.to_dict_recursive()
        if hasattr(stripe_obj, 'to_dict'):
            return stripe_obj.to_dict()
        return stripe_obj

    def _user_for(self, *, client_reference_id=None, metadata=None, customer_id=None):
        """Every event handled here carries one of these ways back to a user."""
        user_model = get_user_model()
        user_id = client_reference_id or (metadata or {}).get('user_id')
        if user_id:
            return user_model.objects.filter(pk=user_id).first()
        if customer_id:
            subscription = Subscription.objects.filter(stripe_customer_id=customer_id).first()
            return subscription.user if subscription else None
        return None

    def _handle_async_payment_succeeded(self, session):
        """A checkout whose payment cleared after it completed (bank transfers,
        some wallets). Only a company top-up waits for this today; the plan
        and credit checkouts take cards, which clear at completion.
        """
        from commercial.services import payments as commercial_payments

        if commercial_payments.is_funds_checkout(session):
            commercial_payments.handle_checkout_completed(session)
        else:
            logger.info('Async payment succeeded for checkout %s (unhandled)', session.get('id'))

    def _handle_checkout_completed(self, session):
        from commercial.services import payments as commercial_payments
        from enterprise import services as enterprise_services

        if commercial_payments.is_funds_checkout(session):
            # A company adding to its ad balance (commercial app), not a
            # person's plan or credits: its own customer, its own ledger.
            commercial_payments.handle_checkout_completed(session)
            return

        if enterprise_services.is_enterprise_checkout(session):
            # A dynamic, catalog-driven configuration, not a single Plan
            # price - entirely its own entitlement/credit model, so it does
            # not go through the Plan-subscription sync below.
            enterprise_services.handle_checkout_completed(session)
            return

        user = self._user_for(
            client_reference_id=session.get('client_reference_id'),
            metadata=session.get('metadata'),
            customer_id=session.get('customer'),
        )
        if user is None:
            logger.error('Stripe checkout.session.completed with no resolvable user: %s',
                         session.get('id'))
            return

        if session.get('mode') == 'subscription':
            # Retrieve returns another Stripe SDK object, not a dict - same
            # conversion as the top-level event, for the same reason.
            stripe_subscription = stripe.Subscription.retrieve(session['subscription']).to_dict()
            self._sync_subscription(user, stripe_subscription)
            self._send_checkout_success_email(
                user=user,
                session=session,
                stripe_subscription=stripe_subscription,
            )
        elif session.get('mode') == 'payment':
            package_id = (session.get('metadata') or {}).get('credit_package_id')
            if not package_id:
                logger.error('Stripe payment checkout with no credit_package_id: %s',
                             session.get('id'))
                return
            from billing.models import CreditPackage
            package = CreditPackage.objects.filter(pk=package_id).first()
            if package is None:
                logger.error('Unknown credit package %s in checkout %s',
                             package_id, session.get('id'))
                return
            services.credit_wallet(
                user,
                amount_cents=package.credit_cents,
                kind='credit_purchase',
                description=f'Purchased {package.name}',
                stripe_payment_intent_id=session.get('payment_intent') or '',
            )
            self._send_checkout_success_email(
                user=user,
                session=session,
                package=package,
            )

    def _send_checkout_success_email(self, *, user, session, stripe_subscription=None, package=None):
        if not user.email:
            return

        invoice = self._invoice_summary_for_checkout(session)
        amount_cents = invoice.get('amount_cents')
        currency = invoice.get('currency') or str(session.get('currency') or 'usd').lower()

        if amount_cents is None:
            amount_cents = self._coerce_int(session.get('amount_total'))

        if stripe_subscription is not None:
            plan_name = self._plan_name_for_subscription(stripe_subscription) or 'Plan subscription'
            billing_kind = 'plan subscription'
            item_name = plan_name
        else:
            billing_kind = 'credit top-up'
            item_name = package.name if package is not None else 'Credit package'

        purchased_at = timezone.now().strftime('%Y-%m-%d %H:%M %Z')
        amount_display = self._format_money(amount_cents, currency)
        invoice_reference = invoice.get('reference') or (session.get('id') or 'checkout session')

        try:
            send_templated_email(
                'billing_checkout_success',
                to=user.email,
                context={
                    'billing_kind': billing_kind,
                    'item_name': item_name,
                    'amount_display': amount_display,
                    'currency': currency,
                    'invoice_reference': invoice_reference,
                    'invoice_url': invoice.get('url') or '',
                    'purchased_at': purchased_at,
                },
                idempotency_key=f'billing-checkout-{session.get("id")}',
            )
        except EmailError:
            # Billing state is already committed; email failure should not ask
            # Stripe to retry and risk duplicate wallet/subscription effects.
            logger.exception('Checkout completed for user %s but receipt email failed', user.pk)

    def _invoice_summary_for_checkout(self, session):
        invoice_id = session.get('invoice')
        if invoice_id:
            try:
                invoice = self._to_dict(stripe.Invoice.retrieve(invoice_id))
                return {
                    'reference': str(invoice.get('number') or invoice.get('id') or invoice_id),
                    'url': str(invoice.get('hosted_invoice_url') or invoice.get('invoice_pdf') or ''),
                    'amount_cents': self._coerce_int(invoice.get('amount_paid') or invoice.get('total')),
                    'currency': str(invoice.get('currency') or '').lower(),
                }
            except stripe.StripeError:
                logger.exception('Failed retrieving Stripe invoice %s', invoice_id)

        payment_intent_id = session.get('payment_intent')
        if payment_intent_id:
            try:
                payment_intent = self._to_dict(
                    stripe.PaymentIntent.retrieve(payment_intent_id, expand=['latest_charge'])
                )
                latest_charge = payment_intent.get('latest_charge') or {}
                if not isinstance(latest_charge, dict):
                    latest_charge = self._to_dict(latest_charge) or {}

                return {
                    'reference': str(payment_intent.get('id') or payment_intent_id),
                    'url': str(latest_charge.get('receipt_url') or ''),
                    'amount_cents': self._coerce_int(
                        payment_intent.get('amount_received') or payment_intent.get('amount')
                    ),
                    'currency': str(payment_intent.get('currency') or '').lower(),
                }
            except stripe.StripeError:
                logger.exception('Failed retrieving Stripe payment intent %s', payment_intent_id)

        return {
            'reference': str(session.get('id') or ''),
            'url': '',
            'amount_cents': self._coerce_int(session.get('amount_total')),
            'currency': str(session.get('currency') or '').lower(),
        }

    def _plan_name_for_subscription(self, stripe_subscription):
        try:
            price_id = stripe_subscription['items']['data'][0]['price']['id']
        except (KeyError, IndexError, TypeError):
            return None

        plan = Plan.objects.filter(stripe_price_id=price_id).first()
        return plan.name if plan else None

    def _format_money(self, amount_cents, currency):
        if amount_cents is None:
            return 'Amount unavailable'

        normalized_currency = str(currency or 'usd').upper()
        return f'${amount_cents / 100:.2f} {normalized_currency}'

    def _handle_subscription_updated(self, stripe_subscription):
        from enterprise import services as enterprise_services

        if enterprise_services.is_enterprise_subscription(stripe_subscription.get('id')):
            enterprise_services.handle_subscription_updated(stripe_subscription)
            return

        user = self._user_for(customer_id=stripe_subscription.get('customer'))
        if user is None:
            logger.error('Stripe subscription event with no resolvable user: %s',
                         stripe_subscription.get('id'))
            return
        self._sync_subscription(user, stripe_subscription)

    def _handle_subscription_deleted(self, stripe_subscription):
        from enterprise import services as enterprise_services

        if enterprise_services.is_enterprise_subscription(stripe_subscription.get('id')):
            enterprise_services.handle_subscription_deleted(stripe_subscription)
            return

        user = self._user_for(customer_id=stripe_subscription.get('customer'))
        if user is None:
            logger.error('Stripe subscription.deleted with no resolvable user: %s',
                         stripe_subscription.get('id'))
            return
        services.downgrade_to_free(user)

    def _handle_invoice_paid(self, invoice):
        from enterprise import services as enterprise_services
        enterprise_services.handle_invoice_paid(invoice)

    def _handle_invoice_payment_failed(self, invoice):
        from enterprise import services as enterprise_services
        enterprise_services.handle_invoice_payment_failed(invoice)

    def _handle_charge_refunded(self, charge):
        from enterprise import services as enterprise_services
        enterprise_services.handle_charge_refunded(charge)

    def _sync_subscription(self, user, stripe_subscription):
        price_id = stripe_subscription['items']['data'][0]['price']['id']
        plan_code = services.plan_code_for_stripe_price(price_id)
        if plan_code is None:
            logger.error('Stripe price %s does not match any Plan', price_id)
            return

        services.activate_subscription(
            user,
            plan_code=plan_code,
            stripe_subscription_id=stripe_subscription['id'],
            status=stripe_subscription['status'],
            current_period_end=services.stripe_timestamp_to_datetime(
                stripe_subscription.get('current_period_end')
            ),
            cancel_at_period_end=stripe_subscription.get('cancel_at_period_end', False),
        )
