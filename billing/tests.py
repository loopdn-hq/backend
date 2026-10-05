"""Tests for wallet ledger arithmetic, plan limits, and the Stripe webhook."""
import json
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from billing import services
from billing.limits import check_can_create_group
from billing.models import CreditPackage, Payment, Plan, Subscription, Wallet, WalletTransaction
from chat.models import Conversation, Message, Participant


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
        self.user.email = 'dave@example.com'
        self.user.save(update_fields=['email'])
        event = {
            'id': 'evt_2', 'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_2', 'mode': 'payment', 'client_reference_id': str(self.user.pk),
                'metadata': {'credit_package_id': str(package.pk)},
                'payment_intent': 'pi_2',
                'amount_total': 1000,
                'currency': 'usd',
            }},
        }
        with mock.patch('billing.webhooks.send_templated_email') as mocked_email:
            response = self._post_event(event)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.balance_cents, 1000)
        mocked_email.assert_called_once()
        context = mocked_email.call_args.kwargs['context']
        self.assertEqual(context['billing_kind'], 'credit top-up')
        self.assertEqual(context['item_name'], 'Test Package')

    def test_checkout_completed_subscription_sends_receipt_email(self):
        self.user.email = 'dave@example.com'
        self.user.save(update_fields=['email'])

        premium_price = 'price_premium_email'
        Plan.objects.filter(code=Plan.Code.PREMIUM).update(stripe_price_id=premium_price)

        subscription = services.get_or_create_subscription(self.user)
        subscription.stripe_customer_id = 'cus_1'
        subscription.save(update_fields=['stripe_customer_id', 'updated_at'])

        stripe_subscription = {
            'id': 'sub_checkout_1',
            'customer': 'cus_1',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': int(timezone.now().timestamp()),
            'items': {'data': [{'price': {'id': premium_price}}]},
        }

        event = {
            'id': 'evt_sub_checkout_1',
            'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_sub_1',
                'mode': 'subscription',
                'client_reference_id': str(self.user.pk),
                'subscription': 'sub_checkout_1',
                'amount_total': 999,
                'currency': 'usd',
                'metadata': {'user_id': str(self.user.pk)},
            }},
        }

        stripe_subscription_obj = mock.Mock()
        stripe_subscription_obj.to_dict.return_value = stripe_subscription

        with mock.patch('stripe.Subscription.retrieve', return_value=stripe_subscription_obj), \
             mock.patch('billing.webhooks.send_templated_email') as mocked_email:
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_email.assert_called_once()
        context = mocked_email.call_args.kwargs['context']
        self.assertEqual(context['billing_kind'], 'plan subscription')
        self.assertEqual(context['item_name'], 'Premium')

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
        original = Plan.objects.get(code='premium')
        original_name = original.name
        original_price = original.monthly_price_cents

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
        original.refresh_from_db()
        self.assertEqual(original.name, original_name)
        self.assertEqual(original.monthly_price_cents, original_price)

        plan = Plan.objects.filter(stripe_product_id='prod_plan_1').first()
        self.assertIsNotNone(plan)
        self.assertEqual(plan.name, 'Premium Plus')
        self.assertEqual(plan.monthly_price_cents, 1999)
        self.assertEqual(plan.stripe_price_id, 'price_plan_1')
        self.assertEqual(plan.max_groups, 25)
        self.assertEqual(plan.features, ['Search'])

    def test_price_metadata_syncs_the_calling_fields(self):
        event = {
            'id': 'evt_plan_calls_1',
            'type': 'price.created',
            'data': {'object': {
                'id': 'price_calls_1',
                'product': 'prod_calls_1',
                'unit_amount': 4999,
                'currency': 'usd',
                'recurring': {'interval': 'month'},
                'metadata': {
                    'data': json.dumps({
                        'type': 'plan',
                        'code': 'callsplan',
                        'max_groups': 10,
                        'max_members_per_group': 10,
                        'max_admins_per_group': 5,
                        'storage_gb': 10,
                        'has_calls': False,
                        'max_call_participants': 50,
                        'has_call_recording': True,
                        'call_recording_retention_days': 60,
                    })
                },
            }},
        }
        with mock.patch('stripe.Product.retrieve', return_value={
            'id': 'prod_calls_1', 'name': 'Calls Plan', 'active': True, 'metadata': {},
        }):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan = Plan.objects.get(stripe_product_id='prod_calls_1')
        self.assertFalse(plan.has_calls)
        self.assertFalse(plan.has_screen_share)
        self.assertEqual(plan.max_call_participants, 50)
        self.assertTrue(plan.has_call_recording)
        self.assertEqual(plan.call_recording_retention_days, 60)

    def test_screen_share_is_bundled_with_calling_and_not_independently_settable(self):
        """Screen share comes free with calling. A metadata key cannot turn it
        off on its own - there is no such key at all."""
        event = {
            'id': 'evt_plan_calls_3',
            'type': 'price.created',
            'data': {'object': {
                'id': 'price_calls_3',
                'product': 'prod_calls_3',
                'unit_amount': 1999,
                'currency': 'usd',
                'recurring': {'interval': 'month'},
                'metadata': {
                    'data': json.dumps({
                        'type': 'plan', 'code': 'bundledscreen',
                        'max_groups': 10, 'max_members_per_group': 10,
                        'max_admins_per_group': 5, 'storage_gb': 10,
                        'has_calls': True,
                        # No screen-share key exists to read - this is here to
                        # prove an attempt to set one has no effect.
                        'has_screen_share': False,
                    })
                },
            }},
        }
        with mock.patch('stripe.Product.retrieve', return_value={
            'id': 'prod_calls_3', 'name': 'Bundled Screen Share', 'active': True, 'metadata': {},
        }):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan = Plan.objects.get(stripe_product_id='prod_calls_3')
        self.assertTrue(plan.has_calls)
        self.assertTrue(plan.has_screen_share)

    def test_metadata_silent_on_calling_leaves_it_on(self):
        """A product whose metadata has never mentioned calling keeps it on.

        Without this, editing a plan's price for an unrelated reason (a
        description change, say) would silently switch calling off for every
        plan whose Stripe metadata predates this feature.
        """
        event = {
            'id': 'evt_plan_calls_2',
            'type': 'price.created',
            'data': {'object': {
                'id': 'price_calls_2',
                'product': 'prod_calls_2',
                'unit_amount': 2999,
                'currency': 'usd',
                'recurring': {'interval': 'month'},
                'metadata': {
                    'data': json.dumps({
                        'type': 'plan', 'code': 'nocallsmeta',
                        'max_groups': 10, 'max_members_per_group': 10,
                        'max_admins_per_group': 5, 'storage_gb': 10,
                    })
                },
            }},
        }
        with mock.patch('stripe.Product.retrieve', return_value={
            'id': 'prod_calls_2', 'name': 'No Calls Metadata', 'active': True, 'metadata': {},
        }):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan = Plan.objects.get(stripe_product_id='prod_calls_2')
        self.assertTrue(plan.has_calls)
        self.assertTrue(plan.has_screen_share)
        self.assertIsNone(plan.max_call_participants)
        self.assertFalse(plan.has_call_recording)
        self.assertIsNone(plan.call_recording_retention_days)

    def test_product_created_without_price_concats_to_existing_plans(self):
        before_count = Plan.objects.count()
        event = {
            'id': 'evt_plan_product_created_1',
            'type': 'product.created',
            'data': {'object': {
                'id': 'prod_plan_new_1',
                'name': 'Starter Plus',
                'active': True,
                'metadata': {
                    'data': json.dumps({
                        'type': 'plan',
                        'code': 'starter_plus',
                        'max_groups': 8,
                        'max_members_per_group': 80,
                        'max_admins_per_group': 2,
                        'storage_gb': 3,
                        'has_whatsapp': True,
                        'has_telegram': False,
                        'features': ['Basic Analytics'],
                        'is_custom_pricing': True,
                    })
                },
            }},
        }

        mocked_prices = mock.Mock()
        mocked_prices.auto_paging_iter.return_value = iter([])
        with mock.patch('stripe.Price.list', return_value=mocked_prices):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Plan.objects.count(), before_count + 1)
        plan = Plan.objects.get(code='starter_plus')
        self.assertEqual(plan.name, 'Starter Plus')
        self.assertEqual(plan.monthly_price_cents, None)
        self.assertTrue(plan.is_custom_pricing)
        self.assertEqual(plan.features, ['Basic Analytics'])

    def test_product_created_with_same_code_does_not_replace_existing_plan(self):
        existing = Plan.objects.create(
            code='basic',
            name='Basic',
            monthly_price_cents=990,
            is_custom_pricing=False,
            active=True,
        )

        event = {
            'id': 'evt_plan_product_created_same_code',
            'type': 'product.created',
            'data': {'object': {
                'id': 'prod_plan_same_code_1',
                'name': 'Premium',
                'active': True,
                'metadata': {
                    'data': json.dumps({
                        'type': 'plan',
                        'code': 'basic',
                        'features': ['Basic Analytics'],
                        'is_custom_pricing': True,
                    })
                },
            }},
        }
        mocked_prices = mock.Mock()
        mocked_prices.auto_paging_iter.return_value = iter([])
        with mock.patch('stripe.Price.list', return_value=mocked_prices):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        existing.refresh_from_db()
        self.assertEqual(existing.name, 'Basic')
        self.assertEqual(existing.monthly_price_cents, 990)

        appended = Plan.objects.filter(stripe_product_id='prod_plan_same_code_1').exclude(pk=existing.pk).first()
        self.assertIsNotNone(appended)
        self.assertEqual(appended.name, 'Premium')
        self.assertNotEqual(appended.code, existing.code)

    def test_price_created_with_same_code_and_new_product_appends_not_replaces(self):
        existing = Plan.objects.create(
            code='basic',
            name='Basic',
            monthly_price_cents=990,
            is_custom_pricing=False,
            active=True,
        )

        event = {
            'id': 'evt_plan_price_same_code_new_product',
            'type': 'price.created',
            'data': {'object': {
                'id': 'price_plan_same_code_new',
                'product': 'prod_plan_same_code_new',
                'unit_amount': 2999,
                'currency': 'usd',
                'recurring': {'interval': 'month'},
                'metadata': {
                    'data': json.dumps({
                        'type': 'plan',
                        'code': 'basic',
                        'features': ['Advanced Analytics'],
                        'is_custom_pricing': False,
                        'max_groups': 50,
                    })
                },
            }},
        }

        with mock.patch('stripe.Product.retrieve', return_value={
            'id': 'prod_plan_same_code_new',
            'name': 'Premium',
            'active': True,
            'metadata': {},
        }):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        existing.refresh_from_db()
        self.assertEqual(existing.name, 'Basic')
        self.assertEqual(existing.monthly_price_cents, 990)

        appended = Plan.objects.filter(stripe_product_id='prod_plan_same_code_new').exclude(pk=existing.pk).first()
        self.assertIsNotNone(appended)
        self.assertEqual(appended.name, 'Premium')
        self.assertEqual(appended.monthly_price_cents, 2999)
        self.assertNotEqual(appended.code, existing.code)

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

    def test_credit_without_id_and_same_name_creates_new_package_for_new_product(self):
        existing = CreditPackage.objects.create(
            name='Promo Credits',
            price_cents=1100,
            credit_cents=1100,
            stripe_product_id='prod_credit_existing',
            stripe_price_id='price_credit_existing',
            active=True,
        )

        event = {
            'id': 'evt_credit_same_name_new_product',
            'type': 'price.created',
            'data': {'object': {
                'id': 'price_credit_new_product',
                'product': 'prod_credit_new',
                'unit_amount': 2500,
                'currency': 'usd',
                'metadata': {
                    'data': json.dumps({'type': 'credit'})
                },
            }},
        }
        with mock.patch('stripe.Product.retrieve', return_value={
            'id': 'prod_credit_new',
            'name': 'Promo Credits',
            'active': True,
            'metadata': {},
        }):
            response = self._post_event(event)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        existing.refresh_from_db()
        self.assertEqual(existing.price_cents, 1100)
        self.assertEqual(existing.stripe_product_id, 'prod_credit_existing')

        created = CreditPackage.objects.filter(stripe_product_id='prod_credit_new').exclude(pk=existing.pk).first()
        self.assertIsNotNone(created)
        self.assertEqual(created.name, 'Promo Credits')
        self.assertEqual(created.price_cents, 2500)

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


class SyncStripeBillingMetadataTests(TestCase):
    """`_plan_metadata` is the push side of the same contract
    `billing.webhooks.StripeWebhook._call_defaults` reads back - see the
    round-trip test below.
    """

    def setUp(self):
        from billing.management.commands.sync_stripe_billing import Command

        self.command = Command()

    def test_calling_fields_are_included(self):
        plan = Plan.objects.get(code=Plan.Code.PREMIUM)
        payload = json.loads(self.command._plan_metadata(plan)['data'])

        self.assertEqual(payload['has_calls'], plan.has_calls)
        self.assertEqual(payload['max_call_participants'], plan.max_call_participants)
        self.assertEqual(payload['has_call_recording'], plan.has_call_recording)
        self.assertEqual(
            payload['call_recording_retention_days'], plan.call_recording_retention_days
        )
        # Never pushed: has_screen_share is derived on the way back in
        # (StripeWebhook._call_defaults), not read from metadata at all.
        self.assertNotIn('has_screen_share', payload)

    def test_round_trips_through_the_webhooks_own_reader(self):
        """What this command pushes, `_call_defaults` reads back unchanged -
        the failure mode this closes is a plan's calling settings silently
        reverting the next time somebody edits its Stripe price for an
        unrelated reason and this webhook fires.
        """
        from billing.webhooks import StripeWebhook

        plan = Plan.objects.get(code=Plan.Code.BUSINESS)
        plan.max_call_participants = 42
        plan.save(update_fields=['max_call_participants'])

        payload = json.loads(self.command._plan_metadata(plan)['data'])
        webhook = StripeWebhook()
        read_back = webhook._call_defaults(payload)

        self.assertEqual(read_back['has_calls'], plan.has_calls)
        self.assertEqual(read_back['max_call_participants'], 42)
        self.assertEqual(read_back['has_call_recording'], plan.has_call_recording)
        self.assertEqual(
            read_back['call_recording_retention_days'], plan.call_recording_retention_days
        )
        self.assertEqual(read_back['has_screen_share'], plan.has_calls)


class BillingAnalyticsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='frank', password='pw')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse('billing:analytics')

    def test_free_plan_returns_no_analytics_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()['data']
        self.assertFalse(data['access']['has_basic_analytics'])
        self.assertFalse(data['access']['has_advanced_analytics'])
        self.assertEqual(data['access']['matched_feature'], 'none')
        self.assertIsNone(data['basic'])
        self.assertIsNone(data['advanced'])
        codes = [item['code'] for item in data['suggestions']]
        self.assertIn('upgrade-analytics-plan', codes)

    def test_basic_plan_returns_basic_analytics_only(self):
        subscription = services.get_or_create_subscription(self.user)
        subscription.plan = Plan.objects.get(code=Plan.Code.PREMIUM)
        subscription.save()

        other = User.objects.create_user(username='gina', password='pw')
        group = Conversation.start_group(self.user, name='Ops')
        group.add_participant(other, role=Participant.Role.MEMBER)
        direct, _ = Conversation.start_direct(self.user, other)

        Message.objects.create(conversation=group, sender=self.user, text='hello team')
        Message.objects.create(conversation=group, sender=other, text='reply')
        Message.objects.create(conversation=direct, sender=other, text='ping')
        services.credit_wallet(
            self.user,
            amount_cents=2000,
            kind=WalletTransaction.Kind.CREDIT_PURCHASE,
            description='top-up',
        )
        services.debit_wallet(self.user, amount_cents=300, description='sms usage')

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()['data']
        self.assertTrue(data['access']['has_basic_analytics'])
        self.assertFalse(data['access']['has_advanced_analytics'])
        self.assertEqual(data['access']['matched_feature'], 'basic')
        self.assertIsNone(data['advanced'])
        self.assertEqual(data['basic']['conversation_counts']['group'], 1)
        self.assertEqual(data['basic']['conversation_counts']['direct'], 1)
        self.assertEqual(data['basic']['message_activity']['sent_last_30_days'], 1)
        self.assertEqual(data['basic']['message_activity']['received_last_30_days'], 2)
        self.assertEqual(data['basic']['plan_info']['code'], Plan.Code.PREMIUM)
        self.assertEqual(data['basic']['wallet']['balance_cents'], 1700)
        self.assertEqual(data['basic']['wallet']['usage_last_30_days']['purchased_cents'], 2000)
        self.assertEqual(data['basic']['wallet']['usage_last_30_days']['used_cents'], 300)

    def test_advanced_plan_returns_advanced_analytics(self):
        advanced_plan = Plan.objects.create(
            code='advanced-pro',
            name='Advanced Pro',
            monthly_price_cents=4999,
            features=['Advanced Analytics'],
            is_custom_pricing=False,
            active=True,
        )
        subscription = services.get_or_create_subscription(self.user)
        subscription.plan = advanced_plan
        subscription.save()

        member_a = User.objects.create_user(username='harry', password='pw')
        member_b = User.objects.create_user(username='irene', password='pw')
        group_a = Conversation.start_group(self.user, name='Alpha')
        group_b = Conversation.start_group(self.user, name='Beta')
        group_b.mode = Conversation.Mode.BROADCAST
        group_b.save(update_fields=['mode'])
        group_a.add_participant(member_a, role=Participant.Role.MEMBER)
        group_a.add_participant(member_b, role=Participant.Role.MEMBER)
        services.credit_wallet(
            self.user,
            amount_cents=5000,
            kind=WalletTransaction.Kind.CREDIT_PURCHASE,
            description='top-up',
        )
        services.debit_wallet(self.user, amount_cents=1200, description='sms usage')

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()['data']
        self.assertTrue(data['access']['has_basic_analytics'])
        self.assertTrue(data['access']['has_advanced_analytics'])
        self.assertEqual(data['access']['matched_feature'], 'advanced')
        self.assertIsNotNone(data['advanced'])
        self.assertEqual(data['advanced']['group_admin_stats']['admin_groups'], 2)
        self.assertEqual(data['advanced']['group_admin_stats']['broadcast_groups'], 1)
        self.assertGreaterEqual(data['advanced']['group_size']['average_active_members'], 1)
        self.assertEqual(data['basic']['plan_info']['code'], 'advanced-pro')
        self.assertEqual(data['advanced']['plan_info']['code'], 'advanced-pro')
        self.assertEqual(data['basic']['wallet']['balance_cents'], 3800)
        self.assertEqual(data['advanced']['wallet']['balance_cents'], 3800)
        self.assertEqual(data['advanced']['wallet']['usage_last_30_days']['purchased_cents'], 5000)
        self.assertEqual(data['advanced']['wallet']['usage_last_30_days']['used_cents'], 1200)


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

    @override_settings(
        CORS_ALLOWED_ORIGINS=['https://loopdn.com', 'tauri://localhost'],
        FRONTEND_URL='https://loopdn.com',
        DESKTOP_ORIGINS=['tauri://localhost', 'http://tauri.localhost'],
        BILLING_ALLOWED_REDIRECT_ORIGINS={'https://loopdn.com'},
    )
    def test_the_desktop_origin_is_not_a_valid_redirect_target(self):
        """The desktop origin is a trusted CALLER, not a payment return target.

        It has to be in CORS_ALLOWED_ORIGINS or neither REST nor the chat
        socket works from the desktop app - and that list is reused to
        validate Stripe redirects. Those two uses must not be conflated:
        Stripe would reject a custom scheme anyway, and the desktop build
        sends the public web origin for checkout precisely because of that.
        """
        payload = {**self.payload, 'success_url': 'tauri://localhost/billing/success'}
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

    def test_dynamic_plan_code_is_accepted(self):
        Plan.objects.create(
            code='premium-3',
            name='Premium 3',
            monthly_price_cents=2999,
            active=True,
            stripe_price_id='price_premium_3',
        )
        payload = {**self.payload, 'plan_code': 'premium-3'}

        with mock.patch(
            'billing.viewsets.v1.billing.services.create_plan_checkout_session',
            return_value={'url': 'https://checkout.stripe.com/test'},
        ):
            response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unknown_plan_code_is_rejected(self):
        payload = {**self.payload, 'plan_code': 'premium-does-not-exist'}
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('plan_code', response.json()['data'])


class TransactionHistoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='hana', password='pw')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse('billing:transactions')

    def _transaction(self, *, days_ago, amount_cents=500,
                     kind=Payment.Kind.CREDIT_PURCHASE, user=None):
        self._references = getattr(self, '_references', 0) + 1
        return Payment.objects.create(
            user=user or self.user, kind=kind, amount_cents=amount_cents,
            description=f'{days_ago} days ago',
            stripe_reference=f'ref_{self._references}',
            paid_at=timezone.now() - timezone.timedelta(days=days_ago),
        )

    def _ids(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row['id'] for row in response.json()['data']['results']]

    def test_preset_ranges_cut_off_at_their_window(self):
        recent = self._transaction(days_ago=1)
        ten = self._transaction(days_ago=10)
        twenty = self._transaction(days_ago=20)
        self._transaction(days_ago=40)

        self.assertEqual(self._ids(range='7d'), [recent.pk])
        self.assertEqual(self._ids(range='15d'), [recent.pk, ten.pk])
        self.assertEqual(self._ids(range='30d'), [recent.pk, ten.pk, twenty.pk])
        # No range at all is the last 30 days.
        self.assertEqual(self._ids(), [recent.pk, ten.pk, twenty.pk])

    def test_custom_range_includes_both_end_days(self):
        self._transaction(days_ago=1)
        ten = self._transaction(days_ago=10)
        forty = self._transaction(days_ago=40)
        self._transaction(days_ago=50)

        today = timezone.localdate()
        ids = self._ids(
            range='custom',
            start_date=(today - timezone.timedelta(days=40)).isoformat(),
            end_date=(today - timezone.timedelta(days=10)).isoformat(),
        )
        self.assertEqual(ids, [ten.pk, forty.pk])

    def test_custom_range_needs_both_dates_in_order(self):
        response = self.client.get(self.url, {'range': 'custom', 'start_date': '2026-01-01'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('end_date', response.json()['data'])

        response = self.client.get(self.url, {
            'range': 'custom', 'start_date': '2026-02-01', 'end_date': '2026-01-01',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_range_is_rejected(self):
        response = self.client.get(self.url, {'range': '90d'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('range', response.json()['data'])

    def test_kind_narrows_the_list(self):
        purchase = self._transaction(days_ago=1)
        plan = self._transaction(days_ago=2, kind=Payment.Kind.PLAN_SUBSCRIPTION)
        enterprise = self._transaction(days_ago=3, kind=Payment.Kind.ENTERPRISE_PLAN)

        self.assertEqual(self._ids(range='7d'), [purchase.pk, plan.pk, enterprise.pk])
        self.assertEqual(self._ids(range='7d', kind='credit_purchase'), [purchase.pk])
        self.assertEqual(self._ids(range='7d', kind='enterprise_plan'), [enterprise.pk])

    def test_only_the_callers_own_transactions_are_listed(self):
        other = User.objects.create_user(username='ivan', password='pw')
        self._transaction(days_ago=1, user=other)
        mine = self._transaction(days_ago=1)

        self.assertEqual(self._ids(range='7d'), [mine.pk])

    def test_signed_out_is_refused(self):
        response = APIClient().get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PaymentRecordingTests(TestCase):
    """Every way Stripe is paid lands one row in the transaction history."""

    def setUp(self):
        self.client = APIClient()
        self.url = reverse('billing:stripe-webhook')
        self.user = User.objects.create_user(username='jude', password='pw')
        subscription = services.get_or_create_subscription(self.user)
        subscription.stripe_customer_id = 'cus_jude'
        subscription.save()

    def _post_event(self, event):
        with mock.patch('stripe.Webhook.construct_event', return_value=event):
            return self.client.post(
                self.url, data=json.dumps(event), content_type='application/json',
                HTTP_STRIPE_SIGNATURE='test',
            )

    def _invoice_paid(self, event_id, **invoice):
        return self._post_event({
            'id': event_id, 'type': 'invoice.paid',
            'data': {'object': {
                'customer': 'cus_jude', 'currency': 'usd',
                'hosted_invoice_url': 'https://invoice.stripe.com/i/test',
                'status_transitions': {'paid_at': 1790000000},
                **invoice,
            }},
        })

    def _enterprise_contract(self, *, stripe_subscription_id):
        from enterprise.models import (
            EnterpriseContract, EnterprisePlanDraft, EnterpriseStripeBilling, PricingCatalogVersion,
        )
        configuration = {
            'limits': {}, 'features': {}, 'credits': {'monthly_messages': 1000},
        }
        catalog = PricingCatalogVersion.objects.create(version=9001)
        draft = EnterprisePlanDraft.objects.create(
            draft_id='draft_jude', account=self.user, configuration=configuration,
            catalog_version=catalog, calculated_subtotal_cents=50000,
            calculated_total_cents=50000,
        )
        contract = EnterpriseContract.objects.create(
            account=self.user, contract_number='ENT-0001', draft=draft,
            configuration_snapshot=configuration, pricing_snapshot={'total_cents': 50000},
            catalog_version=catalog, billing_interval='monthly', status='active',
        )
        EnterpriseStripeBilling.objects.create(
            contract=contract, stripe_customer_id='cus_jude',
            stripe_subscription_id=stripe_subscription_id, status='active',
        )
        return contract

    def test_a_paid_plan_invoice_is_recorded(self):
        Plan.objects.filter(code=Plan.Code.PREMIUM).update(stripe_price_id='price_premium_test')

        response = self._invoice_paid(
            'evt_inv_1', id='in_1', amount_paid=999, subscription='sub_plan',
            lines={'data': [{'price': {'id': 'price_premium_test'}}]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        payment = Payment.objects.get()
        self.assertEqual(payment.user, self.user)
        self.assertEqual(payment.kind, Payment.Kind.PLAN_SUBSCRIPTION)
        self.assertEqual(payment.amount_cents, 999)
        self.assertEqual(payment.description, 'Premium plan')
        self.assertEqual(payment.stripe_reference, 'in_1')
        self.assertEqual(payment.receipt_url, 'https://invoice.stripe.com/i/test')
        self.assertEqual(int(payment.paid_at.timestamp()), 1790000000)

    def test_a_paid_enterprise_invoice_is_recorded_as_enterprise(self):
        self._enterprise_contract(stripe_subscription_id='sub_ent')

        # The newer Stripe shape: the subscription sits under `parent`.
        self._invoice_paid(
            'evt_inv_2', id='in_2', amount_paid=50000,
            parent={'subscription_details': {'subscription': 'sub_ent'}},
        )

        payment = Payment.objects.get()
        self.assertEqual(payment.user, self.user)
        self.assertEqual(payment.kind, Payment.Kind.ENTERPRISE_PLAN)
        self.assertEqual(payment.amount_cents, 50000)
        self.assertEqual(payment.description, 'Enterprise plan (ENT-0001)')

    def test_an_enterprise_invoice_paid_before_its_checkout_is_relabelled(self):
        self._invoice_paid('evt_inv_3', id='in_3', amount_paid=50000, subscription='sub_new')
        self.assertEqual(Payment.objects.get().kind, Payment.Kind.PLAN_SUBSCRIPTION)

        with mock.patch('enterprise.services.handle_checkout_completed'):
            self._post_event({
                'id': 'evt_cs_3', 'type': 'checkout.session.completed',
                'data': {'object': {
                    'id': 'cs_3', 'mode': 'subscription', 'invoice': 'in_3',
                    'metadata': {'kind': 'enterprise_contract', 'contract_number': 'ENT-0002'},
                }},
            })

        payment = Payment.objects.get()
        self.assertEqual(payment.kind, Payment.Kind.ENTERPRISE_PLAN)
        self.assertEqual(payment.description, 'Enterprise plan (ENT-0002)')

    def test_an_invoice_that_charged_nothing_is_not_recorded(self):
        self._invoice_paid('evt_inv_4', id='in_4', amount_paid=0, subscription='sub_plan')
        self.assertFalse(Payment.objects.exists())

    def test_a_credit_top_up_is_recorded_at_the_price_paid(self):
        package = CreditPackage.objects.create(
            name='Promo Package', price_cents=1000, credit_cents=1200,
        )
        self._post_event({
            'id': 'evt_cs_5', 'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_5', 'mode': 'payment', 'client_reference_id': str(self.user.pk),
                'metadata': {'credit_package_id': str(package.pk)},
                'amount_total': 1000, 'currency': 'usd',
            }},
        })

        payment = Payment.objects.get()
        self.assertEqual(payment.kind, Payment.Kind.CREDIT_PURCHASE)
        # The $10 charged, not the $12 of credit it bought.
        self.assertEqual(payment.amount_cents, 1000)
        self.assertEqual(payment.description, 'Purchased Promo Package')
        self.assertEqual(Wallet.objects.get(user=self.user).balance_cents, 1200)

    def test_backfill_records_past_payments_once(self):
        services.credit_wallet(
            self.user, amount_cents=1200, kind=WalletTransaction.Kind.CREDIT_PURCHASE,
            description='Purchased Promo Package', stripe_payment_intent_id='pi_old',
        )
        invoices = mock.Mock()
        invoices.auto_paging_iter.return_value = [{
            'id': 'in_old', 'customer': 'cus_jude', 'amount_paid': 999, 'currency': 'usd',
            'subscription': 'sub_plan', 'created': 1780000000,
        }]
        intent = {
            'id': 'pi_old', 'amount_received': 1000, 'currency': 'usd',
            'latest_charge': {'receipt_url': 'https://pay.stripe.com/receipts/old'},
        }

        with mock.patch('stripe.Invoice.list', return_value=invoices), \
                mock.patch('stripe.PaymentIntent.retrieve', return_value=intent):
            call_command('backfill_billing_payments', stdout=StringIO())
            call_command('backfill_billing_payments', stdout=StringIO())

        self.assertEqual(Payment.objects.count(), 2)
        top_up = Payment.objects.get(stripe_reference='pi_old')
        self.assertEqual(top_up.amount_cents, 1000)
        self.assertEqual(top_up.receipt_url, 'https://pay.stripe.com/receipts/old')
        self.assertEqual(
            Payment.objects.get(stripe_reference='in_old').kind, Payment.Kind.PLAN_SUBSCRIPTION,
        )
