"""Tests for wallet ledger arithmetic, plan limits, and the Stripe webhook."""
import json
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from billing import services
from billing.limits import check_can_create_group
from billing.models import CreditPackage, Plan, Subscription, Wallet, WalletTransaction
from chat.models import Conversation


class WalletLedgerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw')

    def test_credit_then_debit_updates_balance_and_ledger(self):
        services.credit_wallet(
            self.user, amount_cents=1000, kind=WalletTransaction.Kind.CREDIT_PURCHASE,
            description='Purchased Standard Package',
        )
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.balance_cents, 1000)

        services.debit_wallet(self.user, amount_cents=150, description='500 SMS parts')
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance_cents, 850)

        transactions = list(WalletTransaction.objects.filter(wallet=wallet).order_by('created_at'))
        self.assertEqual([t.amount_cents for t in transactions], [1000, -150])
        self.assertEqual(transactions[-1].balance_after_cents, 850)

    def test_debit_beyond_balance_raises_and_does_not_write(self):
        services.credit_wallet(
            self.user, amount_cents=100, kind=WalletTransaction.Kind.CREDIT_PURCHASE,
        )
        with self.assertRaises(services.InsufficientCredit):
            services.debit_wallet(self.user, amount_cents=200)

        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.balance_cents, 100)
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 1)


class SubscriptionDefaultsTests(TestCase):
    def test_new_user_defaults_to_free_plan(self):
        user = User.objects.create_user(username='bob', password='pw')
        subscription = services.get_or_create_subscription(user)
        self.assertEqual(subscription.plan.code, Plan.Code.FREE)
        self.assertEqual(subscription.status, Subscription.Status.FREE)
        self.assertTrue(subscription.is_active())


class PlanLimitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='carol', password='pw')

    def test_free_plan_blocks_a_fourth_group(self):
        free_plan = Plan.objects.get(code=Plan.Code.FREE)
        for _ in range(free_plan.max_groups):
            Conversation.objects.create(
                kind=Conversation.Kind.GROUP, created_by=self.user, name='g',
            )
        with self.assertRaises(Exception):
            check_can_create_group(self.user)

    def test_premium_plan_raises_the_cap(self):
        subscription = services.get_or_create_subscription(self.user)
        subscription.plan = Plan.objects.get(code=Plan.Code.PREMIUM)
        subscription.save()

        for _ in range(4):
            Conversation.objects.create(
                kind=Conversation.Kind.GROUP, created_by=self.user, name='g',
            )
        # Does not raise - 4 is well under Premium's 25.
        check_can_create_group(self.user)


class StripeWebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('billing:stripe-webhook')
        self.user = User.objects.create_user(username='dave', password='pw')

    def _post_event(self, event):
        with mock.patch('stripe.Webhook.construct_event', return_value=event):
            return self.client.post(
                self.url, data=json.dumps(event), content_type='application/json',
                HTTP_STRIPE_SIGNATURE='test',
            )

    def test_duplicate_event_is_a_no_op(self):
        event = {
            'id': 'evt_1', 'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_1', 'mode': 'payment', 'client_reference_id': str(self.user.pk),
                'metadata': {}, 'payment_intent': 'pi_1',
            }},
        }
        # No credit_package_id metadata - the handler logs and returns, but
        # the event is still recorded so a retry of the SAME event dedupes.
        response = self._post_event(event)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self._post_event(event)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()['duplicate'])

    def test_checkout_completed_credits_the_wallet(self):
        package = CreditPackage.objects.create(
            name='Test Package', price_cents=1000, credit_cents=1000,
        )
        event = {
            'id': 'evt_2', 'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_2', 'mode': 'payment', 'client_reference_id': str(self.user.pk),
                'metadata': {'credit_package_id': str(package.pk)},
                'payment_intent': 'pi_2',
            }},
        }
        response = self._post_event(event)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.balance_cents, 1000)

    def test_subscription_updated_activates_the_plan(self):
        premium_price = 'price_premium_test'
        Plan.objects.filter(code=Plan.Code.PREMIUM).update(stripe_price_id=premium_price)
        subscription = services.get_or_create_subscription(self.user)
        subscription.stripe_customer_id = 'cus_1'
        subscription.save()

        event = {
            'id': 'evt_3', 'type': 'customer.subscription.updated',
            'data': {'object': {
                'id': 'sub_1', 'customer': 'cus_1', 'status': 'active',
                'cancel_at_period_end': False,
                'current_period_end': int(timezone.now().timestamp()),
                'items': {'data': [{'price': {'id': premium_price}}]},
            }},
        }
        response = self._post_event(event)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan.code, Plan.Code.PREMIUM)
        self.assertEqual(subscription.status, 'active')

    def test_bad_signature_is_rejected(self):
        import stripe
        with mock.patch(
            'stripe.Webhook.construct_event',
            side_effect=stripe.SignatureVerificationError('bad sig', 'sig_header'),
        ):
            response = self.client.post(
                self.url, data='{}', content_type='application/json',
                HTTP_STRIPE_SIGNATURE='bad',
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_price_created_with_plan_metadata_upserts_plan(self):
        event = {
            'id': 'evt_plan_price_1',
            'type': 'price.created',
            'data': {'object': {
                'id': 'price_plan_1',
                'product': 'prod_plan_1',
                'unit_amount': 1999,
                'currency': 'usd',
                'recurring': {'interval': 'month'},
                'metadata': {
                    'data': json.dumps({
                        'type': 'plan',
                        'code': 'premium',
                        'max_groups': 25,
                        'max_members_per_group': 250,
                        'max_admins_per_group': 5,
                        'storage_gb': 10,
                        'has_whatsapp': True,
                        'has_telegram': True,
                        'features': ['Search'],
                        'is_custom_pricing': False,
                    })
                },
            }},
        }
        with mock.patch('stripe.Product.retrieve', return_value={
            'id': 'prod_plan_1',
            'name': 'Premium Plus',
            'active': True,
            'metadata': {},
        }):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan = Plan.objects.get(code='premium')
        self.assertEqual(plan.name, 'Premium Plus')
        self.assertEqual(plan.monthly_price_cents, 1999)
        self.assertEqual(plan.stripe_price_id, 'price_plan_1')
        self.assertEqual(plan.max_groups, 25)
        self.assertEqual(plan.features, ['Search'])

    def test_product_updated_with_credit_metadata_upserts_credit_package(self):
        event = {
            'id': 'evt_credit_product_1',
            'type': 'product.updated',
            'data': {'object': {
                'id': 'prod_credit_1',
                'name': 'Promo Credits',
                'active': True,
                'metadata': {
                    'data': json.dumps({'type': 'credit', 'id': 901})
                },
            }},
        }
        mocked_prices = mock.Mock()
        mocked_prices.auto_paging_iter.return_value = iter([{
            'id': 'price_credit_1',
            'product': 'prod_credit_1',
            'unit_amount': 2500,
            'currency': 'usd',
            'metadata': {},
        }])
        with mock.patch('stripe.Price.list', return_value=mocked_prices):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        package = CreditPackage.objects.get(pk=901)
        self.assertEqual(package.name, 'Promo Credits')
        self.assertEqual(package.price_cents, 2500)
        self.assertEqual(package.credit_cents, 2500)
        self.assertEqual(package.stripe_product_id, 'prod_credit_1')
        self.assertEqual(package.stripe_price_id, 'price_credit_1')

    def test_credit_payload_without_id_matches_by_stripe_product_id(self):
        before_count = CreditPackage.objects.count()
        package = CreditPackage.objects.create(
            name='Starter Package',
            price_cents=500,
            credit_cents=500,
            stripe_product_id='prod_credit_optional',
            stripe_price_id='price_old',
            active=True,
        )
        original_pk = package.pk

        event = {
            'id': 'evt_credit_optional_id_1',
            'type': 'price.updated',
            'data': {'object': {
                'id': 'price_credit_optional_new',
                'product': 'prod_credit_optional',
                'unit_amount': 1500,
                'currency': 'usd',
                'metadata': {
                    'data': json.dumps({'type': 'credit'})
                },
            }},
        }
        with mock.patch('stripe.Product.retrieve', return_value={
            'id': 'prod_credit_optional',
            'name': 'Starter Package Updated',
            'active': True,
            'metadata': {},
        }):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(CreditPackage.objects.count(), before_count + 1)
        package.refresh_from_db()
        self.assertEqual(package.pk, original_pk)
        self.assertEqual(package.name, 'Starter Package Updated')
        self.assertEqual(package.price_cents, 1500)
        self.assertEqual(package.credit_cents, 1500)
        self.assertEqual(package.stripe_product_id, 'prod_credit_optional')
        self.assertEqual(package.stripe_price_id, 'price_credit_optional_new')

    def test_product_deleted_deactivates_matching_plan_and_credit_package(self):
        plan = Plan.objects.get(code=Plan.Code.PREMIUM)
        plan.active = True
        plan.stripe_price_id = 'price_plan_dead'
        plan.save(update_fields=['active', 'stripe_price_id', 'updated_at'])

        package = CreditPackage.objects.create(
            name='Delete Me',
            price_cents=1000,
            credit_cents=1000,
            active=True,
            stripe_price_id='price_credit_dead',
        )

        event = {
            'id': 'evt_product_deleted_1',
            'type': 'product.deleted',
            'data': {'object': {'id': 'prod_deleted_1', 'deleted': True}},
        }

        def retrieve_price(price_id):
            mapping = {
                'price_plan_dead': {'id': 'price_plan_dead', 'product': 'prod_deleted_1'},
                'price_credit_dead': {'id': 'price_credit_dead', 'product': 'prod_deleted_1'},
            }
            return mapping.get(price_id, {'id': price_id, 'product': 'prod_other'})

        with mock.patch('stripe.Price.retrieve', side_effect=retrieve_price):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        package.refresh_from_db()
        self.assertFalse(plan.active)
        self.assertFalse(package.active)

    def test_product_deleted_with_no_corresponding_rows_is_noop(self):
        plan = Plan.objects.get(code=Plan.Code.BUSINESS)
        plan.active = True
        plan.stripe_price_id = 'price_plan_keep'
        plan.save(update_fields=['active', 'stripe_price_id', 'updated_at'])

        event = {
            'id': 'evt_product_deleted_2',
            'type': 'product.deleted',
            'data': {'object': {'id': 'prod_missing', 'deleted': True}},
        }

        with mock.patch('stripe.Price.retrieve', return_value={
            'id': 'price_plan_keep',
            'product': 'prod_other',
        }):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        self.assertTrue(plan.active)

    def test_product_archived_deactivates_matching_rows(self):
        plan = Plan.objects.get(code=Plan.Code.PREMIUM)
        plan.active = True
        plan.stripe_price_id = 'price_plan_archived'
        plan.save(update_fields=['active', 'stripe_price_id', 'updated_at'])

        package = CreditPackage.objects.create(
            name='Archive Me',
            price_cents=1200,
            credit_cents=1200,
            active=True,
            stripe_product_id='prod_archived_1',
            stripe_price_id='price_credit_archived',
        )

        event = {
            'id': 'evt_product_archived_1',
            'type': 'product.updated',
            'data': {'object': {'id': 'prod_archived_1', 'active': False}},
        }

        def retrieve_price(price_id):
            mapping = {
                'price_plan_archived': {'id': 'price_plan_archived', 'product': 'prod_archived_1'},
                'price_credit_archived': {'id': 'price_credit_archived', 'product': 'prod_archived_1'},
            }
            return mapping.get(price_id, {'id': price_id, 'product': 'prod_other'})

        with mock.patch('stripe.Price.retrieve', side_effect=retrieve_price):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        package.refresh_from_db()
        self.assertFalse(plan.active)
        self.assertFalse(package.active)


@override_settings(BILLING_ALLOWED_REDIRECT_ORIGINS={'https://loopdn.com'})
class PlanCheckoutSecurityTests(TestCase):
    """success_url/cancel_url validation and the checkout rate limit."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='erin', password='pw')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse('billing:checkout-plan')
        Plan.objects.filter(code=Plan.Code.PREMIUM).update(
            stripe_price_id='price_premium_test'
        )
        self.payload = {
            'plan_code': 'premium',
            'success_url': 'https://loopdn.com/billing/success',
            'cancel_url': 'https://loopdn.com/billing/cancel',
        }

    def test_an_untrusted_redirect_origin_is_rejected(self):
        payload = {**self.payload, 'success_url': 'https://evil.example/steal'}
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('success_url', response.json()['data'])

    def test_a_trusted_redirect_origin_passes_validation(self):
        # Mocks the whole checkout-session call, not just the low-level Stripe
        # request under it - get_or_create_stripe_customer would otherwise
        # still hit the real Stripe API and fail wherever STRIPE_SECRET_KEY
        # is unset (CI), for a reason this test has nothing to do with. This
        # only proves the redirect-URL check itself let the request through.
        with mock.patch(
            'billing.viewsets.v1.billing.services.create_plan_checkout_session',
            return_value={'url': 'https://checkout.stripe.com/test'},
        ):
            response = self.client.post(self.url, self.payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_checkout_is_rate_limited(self):
        # DRF's throttle classes bind THROTTLE_RATES from settings once at
        # import time, so override_settings can't reach them here - this
        # exercises the real configured rate (settings.REST_FRAMEWORK
        # ['DEFAULT_THROTTLE_RATES']['billing_checkout']) rather than a
        # smaller one, and asserts one call past it is refused.
        limit = int(settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['billing_checkout'].split('/')[0])
        with mock.patch(
            'billing.viewsets.v1.billing.services.create_plan_checkout_session',
            return_value={'url': 'https://checkout.stripe.com/test'},
        ):
            for _ in range(limit):
                response = self.client.post(self.url, self.payload, format='json')
                self.assertEqual(response.status_code, status.HTTP_200_OK)
            response = self.client.post(self.url, self.payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
