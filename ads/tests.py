"""The ads models, before anything uses them.

There is no service yet, so these hold the schema to what
docs/advertising_integration_plan.md promises of it: the money cannot go wrong,
deleting things cannot erase what was delivered or spent, and ads stay off
until somebody switches them on.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from ads.models import (
    AdImpression,
    AdSettings,
    AdTransaction,
    Advertiser,
    Campaign,
    Creative,
    TargetingCriteria,
)
from chat.models import Conversation, GroupCategory
from chat.services import create_message

User = get_user_model()


class AdsTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(username='joe', password='pw')
        self.advertiser = Advertiser.objects.create(owner=self.owner, name="Joe's Pizza")
        self.campaign = Campaign.objects.create(
            advertiser=self.advertiser,
            name='Lunch deal',
            objective=Campaign.Objective.CALL,
            total_budget_cents=100_000,
        )
        self.creative = Creative.objects.create(
            campaign=self.campaign,
            headline='Lunch for two',
            body='Half price until 2pm.',
            cta_label='Call now',
            cta_kind=Creative.CtaKind.CALL,
        )

    def assertRefused(self, write):
        """The database itself refuses this write - not a serializer, not a view."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            write()


class GroupCategorySeedTests(TestCase):
    def test_the_categories_are_the_product_plans_in_its_order(self):
        self.assertEqual(
            list(GroupCategory.objects.values_list('slug', flat=True)),
            [
                'family-friends',
                'school',
                'religious',
                'neighborhood',
                'community',
                'nonprofit',
                'business',
            ],
        )


class AdSettingsTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_the_switch_ships_turned_off(self):
        self.assertFalse(AdSettings.objects.get().serving_enabled)
        self.assertFalse(AdSettings.serving_is_on())

    def test_no_row_means_no_ads(self):
        """Fails closed, unlike SmsSettings: ads must never appear by accident."""
        AdSettings.objects.all().delete()
        cache.clear()
        self.assertFalse(AdSettings.serving_is_on())

    def test_switching_it_on_is_seen_at_once_despite_the_cache(self):
        self.assertFalse(AdSettings.serving_is_on())  # now cached

        row = AdSettings.objects.get()
        row.serving_enabled = True
        row.save()

        self.assertTrue(AdSettings.serving_is_on())


class MoneyTests(AdsTestCase):
    def test_a_balance_cannot_go_negative(self):
        self.advertiser.balance_cents = -1
        self.assertRefused(self.advertiser.save)

    def test_two_advertisers_cannot_share_a_stripe_customer(self):
        self.advertiser.stripe_customer_id = 'cus_123'
        self.advertiser.save()
        self.assertRefused(
            lambda: Advertiser.objects.create(
                owner=self.owner, name='Second shop', stripe_customer_id='cus_123'
            )
        )

    def test_advertisers_with_no_stripe_customer_yet_do_not_collide(self):
        Advertiser.objects.create(owner=self.owner, name='Second shop')
        Advertiser.objects.create(owner=self.owner, name='Third shop')

    def test_a_purchase_must_add_money(self):
        self.assertRefused(
            lambda: AdTransaction.objects.create(
                advertiser=self.advertiser,
                kind=AdTransaction.Kind.CREDIT_PURCHASE,
                amount_cents=-500,
                balance_after_cents=0,
            )
        )

    def test_spending_must_say_which_campaign_spent_it(self):
        self.assertRefused(
            lambda: AdTransaction.objects.create(
                advertiser=self.advertiser,
                kind=AdTransaction.Kind.USAGE_DEBIT,
                amount_cents=-500,
                balance_after_cents=0,
            )
        )

    def test_the_same_payment_cannot_be_credited_twice(self):
        """A Stripe webhook retried after it was handled must not pay out twice."""
        def credit():
            AdTransaction.objects.create(
                advertiser=self.advertiser,
                kind=AdTransaction.Kind.CREDIT_PURCHASE,
                amount_cents=5_000,
                balance_after_cents=5_000,
                stripe_payment_intent_id='pi_123',
            )

        credit()
        self.assertRefused(credit)

    def test_a_campaign_that_has_spent_cannot_be_deleted(self):
        AdTransaction.objects.create(
            advertiser=self.advertiser,
            campaign=self.campaign,
            kind=AdTransaction.Kind.USAGE_DEBIT,
            amount_cents=-500,
            balance_after_cents=0,
        )
        with self.assertRaises(ProtectedError):
            self.campaign.delete()


class CampaignTests(AdsTestCase):
    def test_a_daily_budget_cannot_exceed_the_total(self):
        self.campaign.daily_budget_cents = 100_001
        self.assertRefused(self.campaign.save)

    def test_a_campaign_ends_after_it_starts(self):
        now = timezone.now()
        self.campaign.starts_at = now
        self.campaign.ends_at = now
        self.assertRefused(self.campaign.save)

    def test_a_call_cta_carries_no_number_of_its_own(self):
        """It uses the campaign's tracking number, so no call escapes measurement."""
        self.creative.cta_target = '+639170000000'
        self.assertRefused(self.creative.save)

    def test_a_link_cta_needs_somewhere_to_go(self):
        self.creative.cta_kind = Creative.CtaKind.LINK
        self.assertRefused(self.creative.save)

    def test_targeting_cannot_want_bigger_groups_than_it_allows(self):
        self.assertRefused(
            lambda: TargetingCriteria.objects.create(
                campaign=self.campaign, min_group_size=50, max_group_size=10
            )
        )


class ImpressionTests(AdsTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(username='maria', password='pw')
        self.group = Conversation.start_group(self.admin, 'Our street')
        self.member = self.group.participants.get(user=self.admin)
        self.message = create_message(self.group, sender=self.admin, text='hello')

    def impression(self):
        return AdImpression.objects.create(
            creative=self.creative,
            conversation=self.group,
            participant=self.member,
            message=self.message,
            channel=AdImpression.Channel.APP,
            cost_cents=12,
        )

    def test_a_member_is_counted_once_per_message_per_channel(self):
        """A retried serve must not bill the advertiser twice for one delivery."""
        self.impression()
        self.assertRefused(self.impression)

    def test_deleting_the_group_keeps_the_record_but_forgets_the_group(self):
        impression = self.impression()

        self.group.delete()

        impression.refresh_from_db()
        self.assertIsNone(impression.conversation_id)
        self.assertIsNone(impression.participant_id)
        self.assertIsNone(impression.message_id)
        self.assertEqual(impression.cost_cents, 12)

    def test_a_creative_that_was_delivered_cannot_be_deleted(self):
        self.impression()
        with self.assertRaises(ProtectedError):
            self.creative.delete()
