"""The commercial app.

Three parts. The schema tests hold the models to what
docs/advertising_integration_plan.md promises of them: the money cannot go
wrong, deleting things cannot erase what was delivered or spent, and ads stay
off until somebody switches them on. The company tests are slice 0 of
docs/campaigns_final_plan.md (section 16): companies, and which one is active.
The campaign tests are slice 1: drafts, audience and reach, creatives, and the
two owner transitions, submit and withdraw.
"""
import io
import itertools
import json
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from commercial.markets import region_codes
from commercial.models import (
    AdImpression,
    AdSettings,
    AdTransaction,
    Campaign,
    CampaignEvent,
    CommercialPreference,
    Company,
    Creative,
    Industry,
    PricingRate,
    TargetingCriteria,
)
from commercial.services import campaigns as campaign_services
from commercial.services import companies as services
from commercial.services import eligibility, pricing, reach, schedule, transitions, wallet
from commercial.services.errors import Invalid, Refused
from chat.models import Conversation, GroupCategory, Participant
from chat.services import create_message
from rbac.services import start_as_commercial

User = get_user_model()

MEDIA_ROOT = tempfile.mkdtemp(prefix='loopd-commercial-media-')


def make_company(owner, name="Joe's Pizza", **fields):
    """A company straight into the database, with every required field filled."""
    fields.setdefault('industry', Industry.objects.get(slug='food-drink'))
    fields.setdefault('country', 'PH')
    fields.setdefault('currency', 'PHP')
    return Company.objects.create(owner=owner, name=name, **fields)


_payments = itertools.count(1)


def fund(company, amount_cents=1_000_000):
    """Money in, as a cleared card payment would put it there."""
    wallet.credit_purchase(
        company,
        amount_cents=amount_cents,
        stripe_payment_intent_id=f'pi_test_{next(_payments)}',
    )
    company.refresh_from_db()


def make_commercial_user(username):
    user = User.objects.create_user(username=username, password='pw')
    start_as_commercial(user)
    return user


def make_logo_upload(name='logo.png'):
    buffer = io.BytesIO()
    Image.new('RGB', (12, 12), color='red').save(buffer, format='PNG')
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type='image/png')


class AdsTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(username='joe', password='pw')
        self.company = make_company(self.owner)
        self.campaign = Campaign.objects.create(
            company=self.company,
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
        self.company.balance_cents = -1
        self.assertRefused(self.company.save)

    def test_two_companies_cannot_share_a_stripe_customer(self):
        self.company.stripe_customer_id = 'cus_123'
        self.company.save()
        self.assertRefused(
            lambda: make_company(
                self.owner, 'Second shop', stripe_customer_id='cus_123'
            )
        )

    def test_companies_with_no_stripe_customer_yet_do_not_collide(self):
        make_company(self.owner, 'Second shop')
        make_company(self.owner, 'Third shop')

    def test_a_purchase_must_add_money(self):
        self.assertRefused(
            lambda: AdTransaction.objects.create(
                company=self.company,
                kind=AdTransaction.Kind.CREDIT_PURCHASE,
                amount_cents=-500,
                balance_after_cents=0,
            )
        )

    def test_spending_must_say_which_campaign_spent_it(self):
        self.assertRefused(
            lambda: AdTransaction.objects.create(
                company=self.company,
                kind=AdTransaction.Kind.CAMPAIGN_SPEND,
                amount_cents=0,
                balance_after_cents=0,
                reserved_change_cents=-500,
                reserved_after_cents=0,
            )
        )

    def test_a_reservation_moves_the_same_money_between_the_buckets(self):
        def reserve(available, reserved):
            AdTransaction.objects.create(
                company=self.company,
                campaign=self.campaign,
                kind=AdTransaction.Kind.CAMPAIGN_RESERVE,
                amount_cents=available,
                balance_after_cents=0,
                reserved_change_cents=reserved,
                reserved_after_cents=500,
            )

        self.assertRefused(lambda: reserve(-500, 400))
        self.assertRefused(lambda: reserve(500, -500))
        reserve(-500, 500)

    def test_neither_bucket_can_go_negative(self):
        self.company.reserved_cents = -1
        self.assertRefused(self.company.save)

    def test_the_same_payment_cannot_be_credited_twice(self):
        """A Stripe webhook retried after it was handled must not pay out twice."""
        def credit():
            AdTransaction.objects.create(
                company=self.company,
                kind=AdTransaction.Kind.CREDIT_PURCHASE,
                amount_cents=5_000,
                balance_after_cents=5_000,
                stripe_payment_intent_id='pi_123',
            )

        credit()
        self.assertRefused(credit)

    def test_a_campaign_that_has_spent_cannot_be_deleted(self):
        AdTransaction.objects.create(
            company=self.company,
            campaign=self.campaign,
            kind=AdTransaction.Kind.CAMPAIGN_SPEND,
            amount_cents=0,
            balance_after_cents=0,
            reserved_change_cents=-500,
            reserved_after_cents=0,
        )
        with self.assertRaises(ProtectedError):
            self.campaign.delete()


class CampaignTests(AdsTestCase):
    def test_a_draft_may_have_no_budget_yet_but_never_a_zero_one(self):
        self.campaign.total_budget_cents = None
        self.campaign.save()
        self.campaign.total_budget_cents = 0
        self.assertRefused(self.campaign.save)

    def test_a_campaign_event_cannot_be_rewritten(self):
        event = CampaignEvent.record(self.campaign, CampaignEvent.Kind.CREATED, actor=self.owner)
        self.assertEqual(event.actor_label, 'joe')
        event.note = 'changed my mind'
        with self.assertRaises(ValueError):
            event.save()

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
        """A retried serve must not bill the company twice for one delivery."""
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


class IndustrySeedTests(TestCase):
    def test_the_list_is_seeded_in_order_with_other_last(self):
        slugs = list(Industry.objects.values_list('slug', flat=True))
        self.assertEqual(slugs[0], 'food-drink')
        self.assertEqual(slugs[-1], 'other')
        self.assertEqual(len(slugs), len(set(slugs)))


class CompanyServiceTests(TestCase):
    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.stranger = make_commercial_user('someone-else')
        self.food = Industry.objects.get(slug='food-drink')

    def create(self, owner=None, **fields):
        fields.setdefault('name', 'Luna Cafe')
        fields.setdefault('industry', self.food)
        fields.setdefault('country', 'PH')
        fields.setdefault('contact_email', 'hello@luna.example')
        return services.create_company(owner or self.owner, **fields)

    def stored_active_id(self, user):
        return (
            CommercialPreference.objects.filter(user=user)
            .values_list('active_company_id', flat=True)
            .first()
        )

    def test_creating_a_company_makes_it_active_and_the_active_one(self):
        company = self.create()
        self.assertEqual(company.owner, self.owner)
        self.assertEqual(company.status, Company.Status.ACTIVE)
        self.assertEqual(self.stored_active_id(self.owner), company.pk)

    def test_the_currency_defaults_from_the_country(self):
        self.assertEqual(self.create(country='PH').currency, 'PHP')
        self.assertEqual(self.create(country='US').currency, 'USD')

    def test_a_company_may_pay_in_another_supported_currency(self):
        self.assertEqual(self.create(country='PH', currency='USD').currency, 'USD')

    def test_the_currency_can_change_until_money_moves(self):
        company = self.create()
        services.update_company(company, currency='USD')
        company.refresh_from_db()
        self.assertEqual(company.currency, 'USD')

        AdTransaction.objects.create(
            company=company,
            kind=AdTransaction.Kind.CREDIT_PURCHASE,
            amount_cents=500,
            balance_after_cents=500,
        )
        with self.assertRaises(services.CurrencyLocked):
            services.update_company(company, currency='PHP')
        company.refresh_from_db()
        self.assertEqual(company.currency, 'USD')

    def test_resending_the_same_currency_after_money_moves_is_not_a_change(self):
        company = self.create()
        AdTransaction.objects.create(
            company=company,
            kind=AdTransaction.Kind.CREDIT_PURCHASE,
            amount_cents=500,
            balance_after_cents=500,
        )
        services.update_company(company, currency='PHP', name='Luna Café')
        company.refresh_from_db()
        self.assertEqual(company.name, 'Luna Café')

    def test_owner_status_and_money_are_not_editable(self):
        company = self.create()
        for field, value in (
            ('owner', self.stranger),
            ('status', Company.Status.SUSPENDED),
            ('balance_cents', 1_000_000),
        ):
            with self.assertRaises(TypeError):
                services.update_company(company, **{field: value})
        company.refresh_from_db()
        self.assertEqual(company.owner, self.owner)
        self.assertEqual(company.status, Company.Status.ACTIVE)
        self.assertEqual(company.balance_cents, 0)

    def test_nobody_with_no_company_has_an_active_one(self):
        self.assertIsNone(services.resolve_active_company(self.owner))
        self.assertFalse(CommercialPreference.objects.filter(user=self.owner).exists())

    def test_the_stored_company_is_the_active_one(self):
        first = self.create(name='Luna Cafe')
        self.create(name='City Fitness')
        services.set_active_company(self.owner, first.pk)
        self.assertEqual(services.resolve_active_company(self.owner), first)

    def test_with_nothing_stored_the_newest_company_is_picked_and_stored(self):
        self.create(name='Luna Cafe')
        newest = self.create(name='City Fitness')
        CommercialPreference.objects.filter(user=self.owner).delete()

        self.assertEqual(services.resolve_active_company(self.owner), newest)
        self.assertEqual(self.stored_active_id(self.owner), newest.pk)

    def test_a_stored_company_they_cannot_manage_falls_back_to_their_own(self):
        mine = self.create(name='Luna Cafe')
        theirs = self.create(owner=self.stranger, name='Not Maria’s')
        CommercialPreference.objects.filter(user=self.owner).update(
            active_company=theirs
        )

        self.assertEqual(services.resolve_active_company(self.owner), mine)
        self.assertEqual(self.stored_active_id(self.owner), mine.pk)

    def test_a_deleted_stored_company_falls_back(self):
        keep = self.create(name='Luna Cafe')
        gone = self.create(name='City Fitness')
        gone.delete()

        self.assertEqual(self.stored_active_id(self.owner), None)
        self.assertEqual(services.resolve_active_company(self.owner), keep)

    def test_switching_to_a_company_they_cannot_manage_changes_nothing(self):
        mine = self.create(name='Luna Cafe')
        theirs = self.create(owner=self.stranger, name='Not Maria’s')

        with self.assertRaises(Company.DoesNotExist):
            services.set_active_company(self.owner, theirs.pk)
        self.assertEqual(self.stored_active_id(self.owner), mine.pk)

    def test_manageable_by_is_their_own_companies_only(self):
        mine = self.create(name='Luna Cafe')
        self.create(owner=self.stranger, name='Not Maria’s')
        self.assertEqual(list(Company.objects.manageable_by(self.owner)), [mine])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class CompanyApiTests(APITestCase):
    list_url = '/api/v1/commercial/companies/'
    active_url = '/api/v1/commercial/active-company/'

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.stranger = make_commercial_user('someone-else')
        self.client.force_authenticate(user=self.owner)

    def detail_url(self, company):
        return f'{self.list_url}{company.pk}/'

    def setup_body(self, **fields):
        body = {
            'name': 'Luna Cafe',
            'industry': 'food-drink',
            'country': 'PH',
            'currency': 'PHP',
            'contact_email': 'hello@luna.example',
        }
        body.update(fields)
        return body

    def data(self, response, expected_status=status.HTTP_200_OK):
        body = response.json()
        self.assertEqual(response.status_code, expected_status, body)
        self.assertEqual(body['code'], 1, body)
        return body['data']

    def refused(self, response, expected_status):
        body = response.json()
        self.assertEqual(response.status_code, expected_status, body)
        self.assertEqual(body['code'], -1, body)
        return body

    # --- who may call it at all ---------------------------------------

    def test_signed_out_is_a_401(self):
        self.client.force_authenticate(user=None)
        self.refused(self.client.get(self.list_url), status.HTTP_401_UNAUTHORIZED)

    def test_an_account_without_commercial_access_gets_nothing(self):
        plain = User.objects.create_user(username='plain', password='pw')
        self.client.force_authenticate(user=plain)
        for url in (
            self.list_url,
            self.active_url,
            '/api/v1/commercial/industries/',
            '/api/v1/commercial/markets/',
        ):
            self.refused(self.client.get(url), status.HTTP_403_FORBIDDEN)
        self.refused(
            self.client.post(self.list_url, self.setup_body(), format='json'),
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(Company.objects.exists())

    # --- creating ------------------------------------------------------

    def test_creating_a_company_returns_it_and_makes_it_active(self):
        created = self.data(
            self.client.post(self.list_url, self.setup_body(), format='json'),
            status.HTTP_201_CREATED,
        )
        self.assertEqual(created['name'], 'Luna Cafe')
        self.assertEqual(created['industry'], 'food-drink')
        self.assertEqual(created['industry_label'], 'Food and drink')
        self.assertEqual(created['status'], 'active')
        self.assertFalse(created['currency_locked'])
        self.assertIsNone(created['logo_url'])

        active = self.data(self.client.get(self.active_url))
        self.assertEqual(active['id'], created['id'])

    def test_the_body_cannot_choose_the_owner_status_or_money(self):
        created = self.data(
            self.client.post(
                self.list_url,
                self.setup_body(
                    owner=self.stranger.pk,
                    status='suspended',
                    balance_cents=1_000_000,
                ),
                format='json',
            ),
            status.HTTP_201_CREATED,
        )
        company = Company.objects.get(pk=created['id'])
        self.assertEqual(company.owner, self.owner)
        self.assertEqual(company.status, Company.Status.ACTIVE)
        self.assertEqual(company.balance_cents, 0)

    def test_setup_needs_its_required_fields(self):
        body = self.refused(
            self.client.post(self.list_url, {}, format='json'),
            status.HTTP_400_BAD_REQUEST,
        )
        for field in ('name', 'industry', 'country', 'contact_email'):
            self.assertIn(field, body['data'])

    def test_only_supported_markets_and_active_industries(self):
        retired = Industry.objects.create(slug='retired', label='Retired', active=False)
        for field, value in (
            ('country', 'GB'),
            ('currency', 'EUR'),
            ('industry', retired.slug),
            ('industry', 'no-such-industry'),
        ):
            body = self.refused(
                self.client.post(
                    self.list_url, self.setup_body(**{field: value}), format='json'
                ),
                status.HTTP_400_BAD_REQUEST,
            )
            self.assertIn(field, body['data'])
        self.assertFalse(Company.objects.exists())

    def test_a_logo_is_uploaded_with_the_form(self):
        created = self.data(
            self.client.post(
                self.list_url,
                self.setup_body(logo=make_logo_upload()),
                format='multipart',
            ),
            status.HTTP_201_CREATED,
        )
        self.assertTrue(created['logo_url'].startswith('http://testserver/media/commercial/media/'))
        self.assertNotIn('logo', created)

    # --- reading and editing -------------------------------------------

    def test_the_list_is_their_companies_only(self):
        mine = make_company(self.owner, 'Luna Cafe')
        make_company(self.stranger, 'Not Maria’s')
        rows = self.data(self.client.get(self.list_url))['results']
        self.assertEqual([row['id'] for row in rows], [mine.pk])

    def test_the_list_comes_ten_at_a_time_by_name(self):
        for number in range(12):
            make_company(self.owner, f'Shop {number:02d}')
        first = self.data(self.client.get(self.list_url))
        self.assertEqual(first['count'], 12)
        self.assertEqual(len(first['results']), 10)
        self.assertEqual(first['results'][0]['name'], 'Shop 00')
        self.assertIsNotNone(first['next'])

        second = self.data(self.client.get(f'{self.list_url}?page=2'))
        self.assertEqual(
            [row['name'] for row in second['results']], ['Shop 10', 'Shop 11']
        )
        self.assertIsNone(second['next'])

    def test_search_narrows_by_name_and_only_among_their_own(self):
        make_company(self.owner, 'Luna Cafe')
        make_company(self.owner, 'City Fitness')
        make_company(self.stranger, 'Luna Rentals')

        found = self.data(self.client.get(f'{self.list_url}?search=luna'))
        self.assertEqual([row['name'] for row in found['results']], ['Luna Cafe'])
        self.assertEqual(found['count'], 1)

        everything = self.data(self.client.get(f'{self.list_url}?search=%20'))
        self.assertEqual(everything['count'], 2)

    def test_somebody_elses_company_is_a_404(self):
        theirs = make_company(self.stranger, 'Not Maria’s')
        self.refused(self.client.get(self.detail_url(theirs)), status.HTTP_404_NOT_FOUND)
        self.refused(
            self.client.patch(self.detail_url(theirs), {'name': 'Mine now'}, format='json'),
            status.HTTP_404_NOT_FOUND,
        )
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, 'Not Maria’s')

    def test_editing_changes_what_setup_set(self):
        company = make_company(self.owner, 'Luna Cafe')
        updated = self.data(
            self.client.patch(
                self.detail_url(company),
                {'name': 'Luna Café', 'industry': 'events', 'website': 'https://luna.example'},
                format='json',
            )
        )
        self.assertEqual(updated['name'], 'Luna Café')
        self.assertEqual(updated['industry'], 'events')
        self.assertEqual(updated['website'], 'https://luna.example')

    def test_contact_phone_address_and_description_are_optional_details(self):
        company = make_company(self.owner, 'Luna Cafe')
        self.assertEqual(company.contact_phone, '')

        updated = self.data(
            self.client.patch(
                self.detail_url(company),
                {
                    'contact_phone': '+639171234567',
                    'address': '12 Rizal St, Cagayan de Oro',
                    'description': 'Specialty coffee and pastries.',
                },
                format='json',
            )
        )
        self.assertEqual(updated['contact_phone'], '+639171234567')
        self.assertEqual(updated['address'], '12 Rizal St, Cagayan de Oro')
        self.assertEqual(updated['description'], 'Specialty coffee and pastries.')

        # And cleared again.
        cleared = self.data(
            self.client.patch(
                self.detail_url(company), {'contact_phone': ''}, format='json'
            )
        )
        self.assertEqual(cleared['contact_phone'], '')

    def test_a_contact_phone_must_be_international_and_a_description_short(self):
        company = make_company(self.owner, 'Luna Cafe')
        for field, value in (
            ('contact_phone', '09171234567'),
            ('description', 'x' * 501),
        ):
            body = self.refused(
                self.client.patch(
                    self.detail_url(company), {field: value}, format='json'
                ),
                status.HTTP_400_BAD_REQUEST,
            )
            self.assertIn(field, body['data'])

    def test_editing_cannot_touch_the_owner_status_or_money(self):
        company = make_company(self.owner, 'Luna Cafe')
        self.data(
            self.client.patch(
                self.detail_url(company),
                {'owner': self.stranger.pk, 'status': 'suspended', 'balance_cents': 999},
                format='json',
            )
        )
        company.refresh_from_db()
        self.assertEqual(company.owner, self.owner)
        self.assertEqual(company.status, Company.Status.ACTIVE)
        self.assertEqual(company.balance_cents, 0)

    def test_the_currency_is_refused_once_money_has_moved(self):
        company = make_company(self.owner, 'Luna Cafe')
        AdTransaction.objects.create(
            company=company,
            kind=AdTransaction.Kind.CREDIT_PURCHASE,
            amount_cents=500,
            balance_after_cents=500,
        )
        self.assertTrue(self.data(self.client.get(self.detail_url(company)))['currency_locked'])
        body = self.refused(
            self.client.patch(self.detail_url(company), {'currency': 'USD'}, format='json'),
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertIn('currency', body['data'])
        company.refresh_from_db()
        self.assertEqual(company.currency, 'PHP')

    def test_there_is_no_put_and_no_delete(self):
        company = make_company(self.owner, 'Luna Cafe')
        self.assertEqual(
            self.client.put(self.detail_url(company), self.setup_body(), format='json').status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.delete(self.detail_url(company)).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())

    # --- the active company --------------------------------------------

    def test_with_no_company_the_active_company_is_null(self):
        self.assertIsNone(self.data(self.client.get(self.active_url)))

    def test_switching_between_their_own_companies(self):
        first = make_company(self.owner, 'Luna Cafe')
        second = make_company(self.owner, 'City Fitness')

        switched = self.data(
            self.client.put(self.active_url, {'company': first.pk}, format='json')
        )
        self.assertEqual(switched['id'], first.pk)
        self.assertEqual(self.data(self.client.get(self.active_url))['id'], first.pk)

        self.client.put(self.active_url, {'company': second.pk}, format='json')
        self.assertEqual(self.data(self.client.get(self.active_url))['id'], second.pk)

    def test_switching_to_somebody_elses_company_is_a_404_and_changes_nothing(self):
        mine = make_company(self.owner, 'Luna Cafe')
        self.client.put(self.active_url, {'company': mine.pk}, format='json')
        theirs = make_company(self.stranger, 'Not Maria’s')

        for company_id in (theirs.pk, 999_999):
            self.refused(
                self.client.put(self.active_url, {'company': company_id}, format='json'),
                status.HTTP_404_NOT_FOUND,
            )
        self.assertEqual(self.data(self.client.get(self.active_url))['id'], mine.pk)

    def test_the_active_company_never_decides_which_company_is_returned(self):
        first = make_company(self.owner, 'Luna Cafe')
        second = make_company(self.owner, 'City Fitness')
        self.client.put(self.active_url, {'company': first.pk}, format='json')

        self.assertEqual(self.data(self.client.get(self.detail_url(second)))['id'], second.pk)
        self.assertEqual(self.data(self.client.get(self.list_url))['count'], 2)

    # --- what the setup form picks from --------------------------------

    def test_industries_are_the_active_ones_in_order(self):
        Industry.objects.create(slug='retired', label='Retired', active=False)
        rows = self.data(self.client.get('/api/v1/commercial/industries/'))
        slugs = [row['slug'] for row in rows]
        self.assertEqual(slugs[0], 'food-drink')
        self.assertEqual(slugs[-1], 'other')
        self.assertNotIn('retired', slugs)

    def test_markets_say_which_currency_each_country_starts_with(self):
        markets = self.data(self.client.get('/api/v1/commercial/markets/'))
        countries = [
            {key: country[key] for key in ('code', 'name', 'default_currency')}
            for country in markets['countries']
        ]
        self.assertIn(
            {'code': 'PH', 'name': 'Philippines', 'default_currency': 'PHP'}, countries
        )
        self.assertIn(
            {'code': 'US', 'name': 'United States', 'default_currency': 'USD'}, countries
        )
        self.assertEqual(
            {row['code'] for row in markets['currencies']}, {'PHP', 'USD'}
        )


# --- slice 1: campaigns -------------------------------------------------------

#: Small floors, so a test audience is a handful of groups rather than hundreds.
SMALL_FLOORS = override_settings(
    MIN_GROUP_SIZE_FOR_ADS=3, MIN_ELIGIBLE_GROUPS=2, MIN_ELIGIBLE_MEMBERS=4
)


_phones = itertools.count(1)

#: A verified number in each country, for a group's creator: the evidence of
#: where an undeclared group is (services/reach.py, creator_in).
_PHONE_FORMAT = {'PH': '+63917{:07d}', 'US': '+1415{:07d}'}


def make_group(name, size, *, category=None, region='', phone_only=0, members=None,
               country='PH'):
    """A group with `size` active members: an admin, account holders, and then
    `phone_only` SMS-only members. `members` adds these accounts instead of new
    ones, for people who are in more than one group.

    Its creator has a verified phone in `country` - None for nobody knows -
    which is where a group that declares no region counts as being.
    """
    admin = User.objects.create_user(username=f'{name}-admin', password='pw')
    if country:
        admin.profile.phone_number = _PHONE_FORMAT[country].format(next(_phones))
        admin.profile.phone_no_verified = True
        admin.profile.save()
    group = Conversation.start_group(admin, name)
    accounts = list(members or []) or [
        User.objects.create_user(username=f'{name}-{i}', password='pw')
        for i in range(size - 1 - phone_only)
    ]
    for user in accounts:
        group.add_participant(user)
    for i in range(phone_only):
        group.add_phone_participant(f'+63917000{i:04d}', f'Member {i}')
    group.category = category
    group.region = region
    group.save(update_fields=['category', 'region'])
    return group


def category(slug):
    return GroupCategory.objects.get(slug=slug)


@SMALL_FLOORS
class ReachTests(TestCase):
    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner)  # in PH

    def estimate(self, **audience):
        return reach.estimate(self.company.country, **audience)

    def test_too_small_says_so_and_gives_no_number_at_all(self):
        make_group('only-one', 5)
        self.assertEqual(
            self.estimate(), {'available': False, 'reason': 'Audience is too small.'}
        )

    def test_a_big_enough_audience_is_ranges_never_counts(self):
        make_group('a', 5)
        make_group('b', 6)

        estimate = self.estimate()

        self.assertTrue(estimate['available'])
        # 2 groups and 11 accounts, given only as the ranges around them.
        self.assertEqual(estimate['groups'], {'low': 1, 'high': 5})
        self.assertEqual(estimate['members'], {'low': 10, 'high': 25})
        self.assertNotIn(2, estimate['groups'].values())
        self.assertNotIn(11, estimate['members'].values())

    def test_groups_under_the_floor_are_never_counted(self):
        make_group('a', 5)
        make_group('tiny', 2)
        self.assertFalse(self.estimate()['available'])

    def test_sms_only_members_size_a_group_but_cannot_see_an_ad(self):
        """Chat ads are seen in the app, so reach counts accounts only."""
        make_group('a', 5, phone_only=4)  # one account: the admin
        make_group('b', 5, phone_only=4)
        self.assertEqual(reach.audience_groups('PH').count(), 2)
        self.assertFalse(self.estimate()['available'])  # 2 accounts < 4

    def test_a_person_in_two_groups_is_one_person(self):
        shared = [User.objects.create_user(username=f'shared-{i}', password='pw') for i in range(2)]
        make_group('a', 3, members=shared)
        make_group('b', 3, members=shared)
        # Two admins and the same two members: four people, not six.
        self.assertEqual(self.estimate()['members'], {'low': 1, 'high': 5})

    def test_undeclared_groups_match_any_but_never_a_named_choice(self):
        make_group('school-1', 5, category=category('school'), region='PH-10')
        make_group('school-2', 5, category=category('school'), region='PH-10')
        make_group('unsaid-1', 5)
        make_group('unsaid-2', 5)

        self.assertEqual(reach.audience_groups('PH').count(), 4)
        self.assertEqual(
            reach.audience_groups('PH', categories=[category('school')]).count(), 2
        )
        self.assertEqual(reach.audience_groups('PH', regions=['PH-10']).count(), 2)

    def test_anywhere_means_the_companys_own_country(self):
        make_group('manila', 5, region='PH-00')
        make_group('whole-ph', 5, region='PH')
        make_group('california', 5, region='US-CA')
        self.assertEqual(reach.audience_groups('PH').count(), 2)
        self.assertEqual(reach.audience_groups('US').count(), 1)

    def test_an_undeclared_group_is_where_its_creators_phone_is(self):
        """Never abroad by default: a group says nothing, its creator's verified
        number does - the rule that picks its SMS number's country.
        """
        make_group('here', 5, country='PH')
        make_group('abroad', 5, country='US')
        make_group('unknown', 5, country=None)

        self.assertEqual(
            list(reach.audience_groups('PH').values_list('name', flat=True)), ['here']
        )
        self.assertEqual(
            list(reach.audience_groups('US').values_list('name', flat=True)), ['abroad']
        )

    def test_a_declared_region_wins_over_the_creators_phone(self):
        make_group('moved', 5, region='US-CA', country='PH')
        self.assertEqual(reach.audience_groups('PH').count(), 0)
        self.assertEqual(reach.audience_groups('US').count(), 1)

    def test_every_option_ticked_is_saved_as_any(self):
        every_category = list(GroupCategory.objects.filter(active=True))
        every_region = sorted(region_codes('PH'))
        audience = reach.clean_audience(
            self.company, categories=every_category, regions=every_region
        )
        self.assertEqual((audience['categories'], audience['regions']), ([], []))

        # Short of every one, the choice stands.
        audience = reach.clean_audience(
            self.company, categories=every_category[:2], regions=every_region[:3]
        )
        self.assertEqual(len(audience['categories']), 2)
        self.assertEqual(len(audience['regions']), 3)

    def test_group_size_limits_narrow_it(self):
        make_group('small', 4)
        make_group('big', 8)
        self.assertEqual(reach.audience_groups('PH', min_group_size=5).count(), 1)
        self.assertEqual(reach.audience_groups('PH', max_group_size=5).count(), 1)

    def test_only_regions_of_the_companys_country(self):
        audience = reach.clean_audience(self.company, regions=['ph-10', 'PH-10'])
        self.assertEqual(audience['regions'], ['PH-10'])
        for code in ('US-CA', 'PH', 'PH-10-01', ''):
            with self.assertRaises(Invalid):
                reach.clean_audience(self.company, regions=[code])

    def test_the_largest_group_size_cannot_be_below_the_smallest(self):
        with self.assertRaises(Invalid):
            reach.clean_audience(self.company, min_group_size=50, max_group_size=10)

    def test_ranges(self):
        self.assertEqual(reach.as_range(1), {'low': 1, 'high': 5})
        self.assertEqual(reach.as_range(100), {'low': 100, 'high': 250})
        self.assertEqual(reach.as_range(249), {'low': 100, 'high': 250})
        self.assertEqual(reach.as_range(2_000_000), {'low': 1_000_000, 'high': None})


@SMALL_FLOORS
class CampaignServiceTests(TestCase):
    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner)
        fund(self.company)
        make_group('a', 5)
        make_group('b', 5)

    def draft(self, **fields):
        fields.setdefault('objective', Campaign.Objective.WEBSITE)
        return campaign_services.create_draft(self.company, self.owner, **fields)

    def ready(self):
        """A draft with every step done, ready to submit."""
        campaign = self.draft(name='Lunch deal')
        campaign_services.set_targeting(campaign)
        campaign_services.update_draft(campaign, total_budget_cents=50_000)
        campaign_services.add_creative(
            campaign,
            headline='Lunch for two',
            body='Half price until 2pm.',
            cta_label='See the menu',
            cta_target='https://luna.example/menu',
        )
        return campaign

    def kinds(self, campaign):
        return list(campaign.events.values_list('kind', flat=True))

    # --- drafts ----------------------------------------------------------

    def test_a_new_campaign_is_a_draft_in_chat_with_its_creation_recorded(self):
        campaign = self.draft()
        self.assertEqual(campaign.status, Campaign.Status.DRAFT)
        self.assertEqual(campaign.placement, Campaign.Placement.CHAT)
        event = campaign.events.get()
        self.assertEqual((event.kind, event.actor), ('created', self.owner))

    def test_call_and_text_stay_hidden(self):
        for objective in (Campaign.Objective.CALL, Campaign.Objective.TEXT):
            with self.assertRaises(Invalid):
                self.draft(objective=objective)
        campaign = self.draft()
        with self.assertRaises(Invalid):
            campaign_services.update_draft(campaign, objective=Campaign.Objective.CALL)

    def test_an_incomplete_draft_saves(self):
        campaign = self.draft()
        campaign_services.update_draft(campaign, daily_budget_cents=1_000)
        campaign.refresh_from_db()
        self.assertEqual((campaign.name, campaign.total_budget_cents), ('', None))
        self.assertEqual(campaign.daily_budget_cents, 1_000)

    def test_rules_between_fields_are_checked_as_soon_as_both_are_set(self):
        campaign = self.draft()
        campaign_services.update_draft(campaign, total_budget_cents=5_000)
        with self.assertRaises(Invalid) as caught:
            campaign_services.update_draft(campaign, daily_budget_cents=6_000)
        self.assertIn('daily_budget_cents', caught.exception.errors)

        start = timezone.now() + timedelta(days=2)
        campaign_services.update_draft(campaign, starts_at=start)
        with self.assertRaises(Invalid) as caught:
            campaign_services.update_draft(campaign, ends_at=start)
        self.assertIn('ends_at', caught.exception.errors)

    def test_status_company_and_placement_are_not_editable(self):
        campaign = self.draft()
        for field, value in (
            ('status', 'running'),
            ('company', make_company(self.owner, 'Other')),
            ('placement', 'chat'),
        ):
            with self.assertRaises(TypeError):
                campaign_services.update_draft(campaign, **{field: value})

    def test_a_draft_with_money_history_cannot_be_deleted(self):
        campaign = self.draft()
        AdTransaction.objects.create(
            company=self.company,
            campaign=campaign,
            kind=AdTransaction.Kind.ADJUSTMENT,
            amount_cents=100,
            balance_after_cents=100,
        )
        with self.assertRaises(Refused):
            campaign_services.delete_draft(campaign)
        self.assertTrue(Campaign.objects.filter(pk=campaign.pk).exists())

    def test_deleting_a_draft_takes_its_history_with_it(self):
        campaign = self.draft()
        campaign_services.delete_draft(campaign)
        self.assertFalse(CampaignEvent.objects.exists())

    # --- creatives -----------------------------------------------------

    def creative(self, campaign, **fields):
        fields.setdefault('headline', 'Lunch for two')
        fields.setdefault('body', 'Half price until 2pm.')
        fields.setdefault('cta_label', 'Go')
        return campaign_services.add_creative(campaign, **fields)

    def test_the_button_comes_from_the_objective(self):
        creative = self.creative(self.draft(), cta_target='https://luna.example')
        self.assertEqual(creative.cta_kind, 'link')

    def test_a_link_must_be_https(self):
        campaign = self.draft()
        for target in ('http://luna.example', 'javascript:alert(1)', 'luna.example', ''):
            with self.assertRaises(Invalid) as caught:
                self.creative(campaign, cta_target=target)
            self.assertIn('cta_target', caught.exception.errors)

    def test_the_button_must_fit_the_objective(self):
        with self.assertRaises(Invalid) as caught:
            self.creative(
                self.draft(), cta_kind='group_join', cta_target='https://loopdn.com/join/'
            )
        self.assertIn('cta_kind', caught.exception.errors)

    @override_settings(FRONTEND_URL='https://loopdn.com')
    def test_a_group_join_goes_to_loopdn_and_nowhere_else(self):
        campaign = self.draft(objective=Campaign.Objective.GROUP_JOIN)
        self.creative(campaign, cta_target='https://loopdn.com/join/?code=ABC234')
        for target in (
            'https://evil.example/loopdn.com',
            'https://loopdn.com.evil.example/',
            'https://user@loopdn.com/',
            'http://loopdn.com/',
        ):
            with self.assertRaises(Invalid):
                self.creative(campaign, cta_target=target)

    def test_a_campaign_has_at_most_five_creatives(self):
        campaign = self.draft()
        for _ in range(campaign_services.MAX_CREATIVES_PER_CAMPAIGN):
            self.creative(campaign, cta_target='https://luna.example')
        with self.assertRaises(Refused):
            self.creative(campaign, cta_target='https://luna.example')

    # --- submit and withdraw -----------------------------------------------

    def test_submit_names_every_missing_piece(self):
        campaign = self.draft()
        with self.assertRaises(transitions.SubmitRefused) as caught:
            transitions.submit(campaign, self.owner)
        self.assertEqual(
            set(caught.exception.errors),
            {'name', 'audience', 'total_budget_cents', 'creatives'},
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.DRAFT)
        self.assertEqual(self.kinds(campaign), ['created'])

    def test_submit_refuses_an_audience_that_is_too_small(self):
        campaign = self.ready()
        campaign_services.set_targeting(campaign, categories=[category('school')])
        with self.assertRaises(transitions.SubmitRefused) as caught:
            transitions.submit(campaign, self.owner)
        self.assertEqual(set(caught.exception.errors), {'audience'})

    def test_submit_refuses_a_start_date_that_has_passed(self):
        campaign = self.ready()
        Campaign.objects.filter(pk=campaign.pk).update(
            starts_at=timezone.now() - timedelta(hours=1)
        )
        with self.assertRaises(transitions.SubmitRefused) as caught:
            transitions.submit(campaign, self.owner)
        self.assertEqual(set(caught.exception.errors), {'starts_at'})

    def test_a_suspended_company_cannot_submit(self):
        campaign = self.ready()
        Company.objects.filter(pk=self.company.pk).update(status=Company.Status.SUSPENDED)
        with self.assertRaises(transitions.SubmitRefused) as caught:
            transitions.submit(campaign, self.owner)
        self.assertEqual(set(caught.exception.errors), {'company'})

    def test_submit_sends_the_campaign_and_its_creatives_to_review(self):
        campaign = transitions.submit(self.ready(), self.owner)

        self.assertEqual(campaign.status, Campaign.Status.PENDING_REVIEW)
        self.assertEqual(
            set(campaign.creatives.values_list('status', flat=True)), {'pending_review'}
        )
        self.assertEqual(self.kinds(campaign), ['created', 'submitted'])

    def test_nothing_reviewed_changes_after_submit(self):
        campaign = transitions.submit(self.ready(), self.owner)
        creative = campaign.creatives.get()

        for change in (
            lambda: campaign_services.update_draft(campaign, name='Changed'),
            lambda: campaign_services.update_draft(campaign, total_budget_cents=1),
            lambda: campaign_services.set_targeting(campaign),
            lambda: self.creative(campaign, cta_target='https://luna.example'),
            lambda: campaign_services.update_creative(creative, headline='Changed'),
            lambda: campaign_services.delete_creative(creative),
            lambda: campaign_services.delete_draft(campaign),
        ):
            with self.assertRaises(campaign_services.CampaignLocked):
                change()

        campaign.refresh_from_db()
        creative.refresh_from_db()
        self.assertEqual((campaign.name, campaign.total_budget_cents), ('Lunch deal', 50_000))
        self.assertEqual(creative.headline, 'Lunch for two')

    def test_an_edit_made_with_a_stale_copy_is_still_refused(self):
        """The view's copy says draft; the row says submitted. The row wins."""
        campaign = self.ready()
        stale = Campaign.objects.get(pk=campaign.pk)
        transitions.submit(campaign, self.owner)
        with self.assertRaises(campaign_services.CampaignLocked):
            campaign_services.update_draft(stale, name='Sneaked in')

    def test_withdraw_returns_it_to_a_draft_that_can_change_again(self):
        campaign = transitions.submit(self.ready(), self.owner)
        campaign = transitions.withdraw(campaign, self.owner)

        self.assertEqual(campaign.status, Campaign.Status.DRAFT)
        self.assertEqual(set(campaign.creatives.values_list('status', flat=True)), {'draft'})
        self.assertEqual(self.kinds(campaign), ['created', 'submitted', 'withdrawn'])
        campaign_services.update_draft(campaign, name='Lunch deal, again')

    def test_every_other_move_is_refused_and_writes_nothing(self):
        draft = self.ready()
        with self.assertRaises(transitions.TransitionRefused):
            transitions.withdraw(draft, self.owner)

        submitted = transitions.submit(self.ready(), self.owner)
        with self.assertRaises(transitions.TransitionRefused):
            transitions.submit(submitted, self.owner)

        for status_ in ('approved', 'running', 'paused', 'ended'):
            Campaign.objects.filter(pk=draft.pk).update(status=status_)
            for move in (transitions.submit, transitions.withdraw):
                with self.assertRaises(transitions.TransitionRefused):
                    move(draft, self.owner)
        self.assertEqual(self.kinds(draft), ['created'])
        self.assertEqual(self.kinds(submitted), ['created', 'submitted'])


@SMALL_FLOORS
@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class CampaignApiTests(APITestCase):
    url = '/api/v1/commercial/campaigns/'

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner, 'Luna Cafe')
        fund(self.company)
        self.stranger = make_commercial_user('someone-else')
        self.strangers_company = make_company(self.stranger, 'City Fitness')
        make_group('a', 5)
        make_group('b', 5)
        self.client.force_authenticate(user=self.owner)

    def data(self, response, expected_status=status.HTTP_200_OK):
        body = response.json()
        self.assertEqual(response.status_code, expected_status, body)
        self.assertEqual(body['code'], 1, body)
        return body['data']

    def refused(self, response, expected_status):
        body = response.json()
        self.assertEqual(response.status_code, expected_status, body)
        self.assertEqual(body['code'], -1, body)
        return body

    def create(self, company=None, **fields):
        body = {'company': (company or self.company).pk, 'objective': 'website'}
        body.update(fields)
        return self.client.post(self.url, body, format='json')

    def detail(self, campaign_id, suffix=''):
        return f'{self.url}{campaign_id}/{suffix}'

    def add_creative(self, campaign_id, **fields):
        body = {
            'headline': 'Lunch for two',
            'body': 'Half price until 2pm.',
            'cta_label': 'See the menu',
            'cta_target': 'https://luna.example/menu',
        }
        body.update(fields)
        return self.client.post(self.detail(campaign_id, 'creatives/'), body, format='multipart')

    def ready(self):
        campaign = self.data(self.create(name='Lunch deal'), status.HTTP_201_CREATED)
        self.data(self.client.put(self.detail(campaign['id'], 'targeting/'), {}, format='json'))
        self.data(
            self.client.patch(
                self.detail(campaign['id']), {'total_budget_cents': 50_000}, format='json'
            )
        )
        self.data(self.add_creative(campaign['id']), status.HTTP_201_CREATED)
        return campaign

    # --- who may call it ------------------------------------------------

    def test_an_account_without_commercial_access_gets_nothing(self):
        plain = User.objects.create_user(username='plain', password='pw')
        self.client.force_authenticate(user=plain)
        for url in (
            f'{self.url}?company={self.company.pk}',
            '/api/v1/commercial/categories/',
        ):
            self.refused(self.client.get(url), status.HTTP_403_FORBIDDEN)
        self.refused(self.create(), status.HTTP_403_FORBIDDEN)
        self.refused(
            self.client.post(
                '/api/v1/commercial/reach-estimate/', {'company': self.company.pk}, format='json'
            ),
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(Campaign.objects.exists())

    def test_somebody_elses_campaign_is_a_404_everywhere(self):
        theirs = campaign_services.create_draft(
            self.strangers_company, self.stranger, objective='website'
        )
        creative = campaign_services.add_creative(
            theirs, headline='h', body='b', cta_label='c', cta_target='https://x.example'
        )
        for response in (
            self.client.get(self.detail(theirs.pk)),
            self.client.patch(self.detail(theirs.pk), {'name': 'Mine now'}, format='json'),
            self.client.delete(self.detail(theirs.pk)),
            self.client.put(self.detail(theirs.pk, 'targeting/'), {}, format='json'),
            self.client.get(self.detail(theirs.pk, 'creatives/')),
            self.add_creative(theirs.pk),
            self.client.post(self.detail(theirs.pk, 'submit/')),
            self.client.post(self.detail(theirs.pk, 'withdraw/')),
            self.client.patch(
                f'/api/v1/commercial/creatives/{creative.pk}/', {'headline': 'x'}, format='json'
            ),
            self.client.delete(f'/api/v1/commercial/creatives/{creative.pk}/'),
        ):
            self.refused(response, status.HTTP_404_NOT_FOUND)

        theirs.refresh_from_db()
        self.assertEqual(theirs.name, '')
        self.assertEqual(theirs.creatives.count(), 1)
        self.assertFalse(TargetingCriteria.objects.filter(campaign=theirs).exists())

    # --- the list and creating -----------------------------------------

    def test_the_list_names_its_company_and_shows_only_that_one(self):
        self.refused(self.client.get(self.url), status.HTTP_400_BAD_REQUEST)
        self.refused(
            self.client.get(f'{self.url}?company={self.strangers_company.pk}'),
            status.HTTP_404_NOT_FOUND,
        )

        second = make_company(self.owner, 'CDO Rentals')
        mine = self.data(self.create(name='Lunch deal'), status.HTTP_201_CREATED)
        self.data(self.create(company=second, name='Room for rent'), status.HTTP_201_CREATED)

        page = self.data(self.client.get(f'{self.url}?company={self.company.pk}'))
        self.assertEqual(page['count'], 1)
        self.assertEqual(page['results'][0]['id'], mine['id'])
        self.assertEqual(page['results'][0]['currency'], 'PHP')
        self.assertEqual(page['results'][0]['country'], 'PH')

    def test_the_list_narrows_by_status_and_name(self):
        lunch = self.data(self.create(name='Lunch deal'), status.HTTP_201_CREATED)
        self.data(self.create(name='Dinner deal'), status.HTTP_201_CREATED)
        Campaign.objects.filter(pk=lunch['id']).update(status='pending_review')
        url = f'{self.url}?company={self.company.pk}'

        page = self.data(self.client.get(f'{url}&status=pending_review'))
        self.assertEqual([c['name'] for c in page['results']], ['Lunch deal'])

        page = self.data(self.client.get(f'{url}&search=DINNER'))
        self.assertEqual([c['name'] for c in page['results']], ['Dinner deal'])

        page = self.data(self.client.get(f'{url}&search=deal&status=draft'))
        self.assertEqual(page['count'], 1)

        body = self.refused(
            self.client.get(f'{url}&status=deleted'), status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('status', body['data'])

    def test_creating_starts_a_draft_for_the_named_company(self):
        created = self.data(self.create(name='Lunch deal'), status.HTTP_201_CREATED)
        self.assertEqual(created['status'], 'draft')
        self.assertEqual(created['placement'], 'chat')
        self.assertEqual(created['company'], self.company.pk)
        self.assertIsNone(created['targeting'])
        self.assertEqual(created['creatives'], [])
        self.assertEqual([e['kind'] for e in created['events']], ['created'])
        self.assertEqual(created['events'][0]['by'], 'maria')

    def test_creating_for_somebody_elses_company_is_a_404(self):
        self.refused(self.create(company=self.strangers_company), status.HTTP_404_NOT_FOUND)
        self.assertFalse(Campaign.objects.exists())

    def test_call_and_text_are_refused(self):
        body = self.refused(self.create(objective='call'), status.HTTP_400_BAD_REQUEST)
        self.assertIn('objective', body['data'])

    # --- editing a draft ---------------------------------------------------

    def test_a_step_saves_and_the_body_cannot_reach_status_or_company(self):
        campaign = self.data(self.create(), status.HTTP_201_CREATED)
        saved = self.data(
            self.client.patch(
                self.detail(campaign['id']),
                {
                    'name': 'Lunch deal',
                    'daily_budget_cents': 1_000,
                    'status': 'running',
                    'company': self.strangers_company.pk,
                    'placement': 'elsewhere',
                },
                format='json',
            )
        )
        self.assertEqual(saved['name'], 'Lunch deal')
        self.assertEqual(saved['daily_budget_cents'], 1_000)
        self.assertEqual(
            (saved['status'], saved['company'], saved['placement']),
            ('draft', self.company.pk, 'chat'),
        )

    def test_a_daily_cap_over_the_total_is_a_field_error(self):
        campaign = self.data(self.create(), status.HTTP_201_CREATED)
        body = self.refused(
            self.client.patch(
                self.detail(campaign['id']),
                {'total_budget_cents': 1_000, 'daily_budget_cents': 2_000},
                format='json',
            ),
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertIn('daily_budget_cents', body['data'])

    def test_there_is_no_put_on_a_campaign(self):
        campaign = self.data(self.create(), status.HTTP_201_CREATED)
        self.refused(
            self.client.put(self.detail(campaign['id']), {'name': 'x'}, format='json'),
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def test_the_audience_is_saved_by_slug_and_region_code(self):
        campaign = self.data(self.create(), status.HTTP_201_CREATED)
        saved = self.data(
            self.client.put(
                self.detail(campaign['id'], 'targeting/'),
                {'categories': ['school', 'religious'], 'regions': ['PH-10'], 'min_group_size': 20},
                format='json',
            )
        )
        self.assertEqual(
            saved['targeting'],
            {
                'categories': ['school', 'religious'],
                'regions': ['PH-10'],
                'min_group_size': 20,
                'max_group_size': None,
            },
        )

        body = self.refused(
            self.client.put(
                self.detail(campaign['id'], 'targeting/'), {'regions': ['US-CA']}, format='json'
            ),
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertIn('regions', body['data'])

    def test_deleting_a_draft(self):
        campaign = self.data(self.create(), status.HTTP_201_CREATED)
        response = self.client.delete(self.detail(campaign['id']))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Campaign.objects.exists())

    # --- creatives -----------------------------------------------------------

    def test_a_creative_is_added_with_its_image_and_edited(self):
        campaign = self.data(self.create(), status.HTTP_201_CREATED)
        creative = self.data(
            self.add_creative(campaign['id'], image=make_logo_upload('ad.png')),
            status.HTTP_201_CREATED,
        )
        self.assertEqual(creative['cta_kind'], 'link')
        self.assertEqual(creative['status'], 'draft')
        self.assertIn('/commercial/media/', creative['image_url'])
        self.assertNotIn('/ads/', creative['image_url'])

        url = f'/api/v1/commercial/creatives/{creative["id"]}/'
        edited = self.data(self.client.patch(url, {'headline': 'Dinner too'}, format='json'))
        self.assertEqual(edited['headline'], 'Dinner too')
        self.assertIsNotNone(edited['image_url'])

        cleared = self.data(self.client.patch(url, {'image': None}, format='json'))
        self.assertIsNone(cleared['image_url'])

    def test_a_creative_needs_an_image_that_is_an_image_and_a_safe_link(self):
        campaign = self.data(self.create(), status.HTTP_201_CREATED)
        not_an_image = SimpleUploadedFile('ad.png', b'not an image', content_type='image/png')
        body = self.refused(
            self.add_creative(campaign['id'], image=not_an_image), status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('image', body['data'])

        body = self.refused(
            self.add_creative(campaign['id'], cta_target='http://luna.example'),
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertIn('cta_target', body['data'])

        body = self.refused(
            self.add_creative(campaign['id'], headline='  '), status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('headline', body['data'])
        self.assertFalse(Creative.objects.exists())

    # --- reach ----------------------------------------------------------------

    def test_reach_is_ranges_or_too_small_and_only_for_your_companies(self):
        url = '/api/v1/commercial/reach-estimate/'
        estimate = self.data(self.client.post(url, {'company': self.company.pk}, format='json'))
        self.assertEqual(estimate['available'], True)
        self.assertEqual(set(estimate['groups']), {'low', 'high'})

        estimate = self.data(
            self.client.post(
                url, {'company': self.company.pk, 'categories': ['school']}, format='json'
            )
        )
        self.assertEqual(estimate, {'available': False, 'reason': 'Audience is too small.'})

        self.refused(
            self.client.post(url, {'company': self.strangers_company.pk}, format='json'),
            status.HTTP_404_NOT_FOUND,
        )

    # --- submit and withdraw ---------------------------------------------------

    def test_submit_lists_what_is_missing(self):
        campaign = self.data(self.create(), status.HTTP_201_CREATED)
        body = self.refused(
            self.client.post(self.detail(campaign['id'], 'submit/')),
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            set(body['data']), {'name', 'audience', 'total_budget_cents', 'creatives'}
        )

    def test_submit_then_locked_then_withdraw(self):
        campaign = self.ready()

        submitted = self.data(self.client.post(self.detail(campaign['id'], 'submit/')))
        self.assertEqual(submitted['status'], 'pending_review')
        self.assertEqual(submitted['creatives'][0]['status'], 'pending_review')
        self.assertEqual([e['kind'] for e in submitted['events']], ['created', 'submitted'])

        body = self.refused(
            self.client.patch(self.detail(campaign['id']), {'name': 'x'}, format='json'),
            status.HTTP_409_CONFLICT,
        )
        self.assertIn('Withdraw', body['message'])
        self.refused(
            self.client.post(self.detail(campaign['id'], 'submit/')), status.HTTP_409_CONFLICT
        )

        withdrawn = self.data(self.client.post(self.detail(campaign['id'], 'withdraw/')))
        self.assertEqual(withdrawn['status'], 'draft')
        self.assertEqual(withdrawn['events'][-1]['kind'], 'withdrawn')

    # --- the lists the builder picks from -------------------------------------

    def test_categories_are_the_active_ones_in_order(self):
        GroupCategory.objects.filter(slug='business').update(active=False)
        categories = self.data(self.client.get('/api/v1/commercial/categories/'))
        self.assertEqual(categories[0], {'slug': 'family-friends', 'label': 'Family and friends'})
        self.assertNotIn('business', [c['slug'] for c in categories])

    def test_markets_list_each_countrys_regions(self):
        markets = self.data(self.client.get('/api/v1/commercial/markets/'))
        ph = next(c for c in markets['countries'] if c['code'] == 'PH')
        self.assertIn({'code': 'PH-10', 'name': 'Northern Mindanao'}, ph['regions'])


# --- slice 3: money -------------------------------------------------------------


@SMALL_FLOORS
class MoneyFlowTests(TestCase):
    """Section 9 of the plan: reserve at submit, release on withdraw, never twice."""

    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner)  # PHP
        make_group('a', 5)
        make_group('b', 5)

    def ready(self, budget_cents=50_000, name='Lunch deal'):
        campaign = campaign_services.create_draft(
            self.company, self.owner, objective=Campaign.Objective.WEBSITE, name=name
        )
        campaign_services.set_targeting(campaign)
        campaign_services.update_draft(campaign, total_budget_cents=budget_cents)
        campaign_services.add_creative(
            campaign, headline='h', body='b', cta_label='Go', cta_target='https://luna.example'
        )
        return campaign

    def buckets(self):
        self.company.refresh_from_db()
        return self.company.balance_cents, self.company.reserved_cents

    def test_the_launch_prices_are_seeded(self):
        self.assertEqual(pricing.rate_in_force('chat', 'PHP').cpm_cents, 5_000)
        self.assertEqual(pricing.rate_in_force('chat', 'USD').cpm_cents, 100)
        self.assertEqual(pricing.views_for(50_000, 5_000), 10_000)

    def test_a_payment_is_credited_once(self):
        wallet.credit_purchase(self.company, amount_cents=80_000, stripe_payment_intent_id='pi_1')
        wallet.credit_purchase(self.company, amount_cents=80_000, stripe_payment_intent_id='pi_1')
        self.assertEqual(self.buckets(), (80_000, 0))
        row = AdTransaction.objects.get()
        self.assertEqual((row.kind, row.amount_cents, row.balance_after_cents), ('credit_purchase', 80_000, 80_000))

    def test_submit_fixes_the_price_and_sets_the_budget_aside(self):
        fund(self.company, 80_000)
        campaign = transitions.submit(self.ready(50_000), self.owner)

        self.assertEqual(self.buckets(), (30_000, 50_000))
        self.assertEqual((campaign.cpm_cents, campaign.reserved_cents), (5_000, 50_000))
        row = AdTransaction.objects.get(kind='campaign_reserve')
        self.assertEqual(
            (row.campaign_id, row.amount_cents, row.reserved_change_cents,
             row.balance_after_cents, row.reserved_after_cents),
            (campaign.pk, -50_000, 50_000, 30_000, 50_000),
        )
        event = campaign.events.get(kind='submitted')
        self.assertEqual(event.metadata, {'cpm_cents': 5_000, 'reserved_cents': 50_000})
        self.assertEqual(campaign.events.count(), 2)  # created, submitted: one per move

    def test_short_of_money_nothing_moves_and_submit_says_how_much(self):
        fund(self.company, 20_000)
        campaign = self.ready(50_000)
        with self.assertRaises(transitions.SubmitRefused) as caught:
            transitions.submit(campaign, self.owner)
        self.assertEqual(set(caught.exception.errors), {'funds'})
        self.assertIn('PHP 300.00', caught.exception.errors['funds'])

        campaign.refresh_from_db()
        self.assertEqual((campaign.status, campaign.cpm_cents, campaign.reserved_cents), ('draft', None, 0))
        self.assertEqual(self.buckets(), (20_000, 0))
        self.assertFalse(AdTransaction.objects.filter(kind='campaign_reserve').exists())

    def test_two_campaigns_cannot_be_promised_the_same_money(self):
        fund(self.company, 60_000)
        first, second = self.ready(50_000, 'First'), self.ready(50_000, 'Second')
        transitions.submit(first, self.owner)
        with self.assertRaises(transitions.SubmitRefused) as caught:
            transitions.submit(second, self.owner)
        self.assertIn('funds', caught.exception.errors)
        self.assertEqual(self.buckets(), (10_000, 50_000))

    def test_withdraw_returns_the_money_and_forgets_the_price(self):
        fund(self.company, 80_000)
        campaign = transitions.submit(self.ready(50_000), self.owner)
        campaign = transitions.withdraw(campaign, self.owner)

        self.assertEqual(self.buckets(), (80_000, 0))
        self.assertEqual((campaign.cpm_cents, campaign.reserved_cents), (None, 0))
        row = AdTransaction.objects.get(kind='campaign_release')
        self.assertEqual((row.amount_cents, row.reserved_change_cents), (50_000, -50_000))
        self.assertEqual(campaign.events.get(kind='withdrawn').metadata, {'released_cents': 50_000})

    def test_a_campaign_keeps_the_price_it_was_submitted_at(self):
        fund(self.company, 200_000)
        campaign = transitions.submit(self.ready(50_000), self.owner)
        PricingRate.objects.create(
            placement='chat', currency='PHP', cpm_cents=9_000, effective_from=timezone.now()
        )

        campaign.refresh_from_db()
        self.assertEqual(campaign.cpm_cents, 5_000)

        # Withdrawn and resubmitted, it takes the price in force then.
        transitions.withdraw(campaign, self.owner)
        campaign = transitions.submit(campaign, self.owner)
        self.assertEqual(campaign.cpm_cents, 9_000)

    def test_no_price_in_the_currency_means_no_submit(self):
        fund(self.company, 80_000)
        PricingRate.objects.filter(currency='PHP').delete()
        with self.assertRaises(transitions.SubmitRefused) as caught:
            transitions.submit(self.ready(50_000), self.owner)
        self.assertEqual(set(caught.exception.errors), {'pricing'})

    def test_a_running_campaigns_budget_only_goes_up_and_reserves_the_difference(self):
        fund(self.company, 100_000)
        campaign = transitions.submit(self.ready(50_000), self.owner)

        with self.assertRaises(transitions.TransitionRefused):
            transitions.increase_budget(campaign, self.owner, 80_000)  # still in review

        Campaign.objects.filter(pk=campaign.pk).update(status='running')
        for smaller in (50_000, 40_000):
            with self.assertRaises(Invalid):
                transitions.increase_budget(campaign, self.owner, smaller)

        campaign = transitions.increase_budget(campaign, self.owner, 80_000)
        self.assertEqual((campaign.total_budget_cents, campaign.reserved_cents), (80_000, 80_000))
        self.assertEqual(self.buckets(), (20_000, 80_000))
        event = campaign.events.get(kind='budget_increased')
        self.assertEqual(event.metadata, {'from_cents': 50_000, 'to_cents': 80_000, 'reserved_cents': 30_000})

        with self.assertRaises(Invalid) as caught:
            transitions.increase_budget(campaign, self.owner, 200_000)
        self.assertIn('funds', caught.exception.errors)
        self.assertEqual(self.buckets(), (20_000, 80_000))


def checkout_completed(company, *, event_id='evt_1', payment_intent='pi_1', amount=100_000,
                       currency='php', paid=True, event_type='checkout.session.completed'):
    """A Stripe event for a finished company top-up, as the webhook receives it."""
    return {
        'id': event_id,
        'type': event_type,
        'data': {'object': {
            'id': f'cs_{event_id}',
            'mode': 'payment',
            'payment_status': 'paid' if paid else 'unpaid',
            'payment_intent': payment_intent,
            'amount_total': amount,
            'currency': currency,
            'metadata': {'kind': 'commercial_funds', 'company_id': str(company.pk), 'amount_cents': str(amount)},
        }},
    }


@SMALL_FLOORS
@override_settings(BILLING_ALLOWED_REDIRECT_ORIGINS={'http://commercial.localhost:3000'})
class MoneyApiTests(APITestCase):
    webhook_url = '/api/v1/billing/webhooks/stripe/'

    def setUp(self):
        cache.clear()  # the checkout throttle counts in the cache
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner, 'Luna Cafe')
        self.stranger = make_commercial_user('someone-else')
        self.strangers_company = make_company(self.stranger, 'City Fitness')
        self.client.force_authenticate(user=self.owner)

    def data(self, response, expected_status=status.HTTP_200_OK):
        body = response.json()
        self.assertEqual(response.status_code, expected_status, body)
        return body['data']

    def checkout(self, company=None, **fields):
        body = {
            'amount_cents': 100_000,
            'success_url': 'http://commercial.localhost:3000/balance/?checkout=success',
            'cancel_url': 'http://commercial.localhost:3000/balance/?checkout=cancelled',
        }
        body.update(fields)
        return self.client.post(
            f'/api/v1/commercial/companies/{(company or self.company).pk}/checkout/',
            body, format='json',
        )

    def post_event(self, event):
        with mock.patch('stripe.Webhook.construct_event', return_value=event):
            return self.client.post(
                self.webhook_url, data=json.dumps(event), content_type='application/json',
                HTTP_STRIPE_SIGNATURE='test',
            )

    def test_pricing_is_the_rate_in_force_for_the_companys_currency(self):
        rate = self.data(self.client.get(f'/api/v1/commercial/pricing/?company={self.company.pk}'))
        self.assertEqual((rate['currency'], rate['cpm_cents']), ('PHP', 5_000))
        self.assertEqual(
            self.client.get(f'/api/v1/commercial/pricing/?company={self.strangers_company.pk}').status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_checkout_opens_a_stripe_payment_on_the_companys_own_customer(self):
        with mock.patch('stripe.Customer.create', return_value={'id': 'cus_luna'}) as create_customer, \
             mock.patch('stripe.checkout.Session.create', return_value={'url': 'https://checkout.stripe.com/x'}) as create_session:
            started = self.data(self.checkout())

        self.assertEqual(started, {'checkout_url': 'https://checkout.stripe.com/x'})
        self.assertEqual(create_customer.call_args.kwargs['name'], 'Luna Cafe')
        kwargs = create_session.call_args.kwargs
        self.assertEqual(kwargs['customer'], 'cus_luna')
        self.assertEqual(kwargs['line_items'][0]['price_data']['currency'], 'php')
        self.assertEqual(kwargs['line_items'][0]['price_data']['unit_amount'], 100_000)
        self.assertEqual(kwargs['metadata']['kind'], 'commercial_funds')
        self.company.refresh_from_db()
        self.assertEqual(self.company.stripe_customer_id, 'cus_luna')
        # Starting a payment moves no money.
        self.assertEqual(self.company.balance_cents, 0)

    def test_checkout_refuses_small_amounts_strange_return_addresses_and_strangers(self):
        with mock.patch('stripe.Customer.create') as create_customer, \
             mock.patch('stripe.checkout.Session.create') as create_session:
            body = self.checkout(amount_cents=49_999).json()
            self.assertIn('amount_cents', body['data'])
            self.assertIn('PHP 500.00', body['message'])
            self.assertEqual(
                self.checkout(success_url='https://evil.example/').status_code,
                status.HTTP_400_BAD_REQUEST,
            )
            self.assertEqual(
                self.checkout(company=self.strangers_company).status_code,
                status.HTTP_404_NOT_FOUND,
            )
        create_customer.assert_not_called()
        create_session.assert_not_called()

    def test_a_paid_checkout_credits_the_company_once(self):
        response = self.post_event(checkout_completed(self.company))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.company.refresh_from_db()
        self.assertEqual(self.company.balance_cents, 100_000)

        # The same event again, and the same payment under a new event id.
        self.post_event(checkout_completed(self.company))
        self.post_event(checkout_completed(self.company, event_id='evt_2'))
        self.company.refresh_from_db()
        self.assertEqual(self.company.balance_cents, 100_000)
        self.assertEqual(AdTransaction.objects.filter(kind='credit_purchase').count(), 1)

    def test_an_unpaid_or_foreign_currency_checkout_credits_nothing(self):
        self.post_event(checkout_completed(self.company, paid=False))
        self.post_event(checkout_completed(self.company, event_id='evt_2', payment_intent='pi_2', currency='usd'))
        self.company.refresh_from_db()
        self.assertEqual(self.company.balance_cents, 0)

        # A delayed payment that clears later arrives as its own event.
        self.post_event(checkout_completed(
            self.company, event_id='evt_3', event_type='checkout.session.async_payment_succeeded'
        ))
        self.company.refresh_from_db()
        self.assertEqual(self.company.balance_cents, 100_000)

    def test_the_ledger_lists_every_movement_newest_first(self):
        fund(self.company, 80_000)
        make_group('a', 5)
        make_group('b', 5)
        campaign = campaign_services.create_draft(self.company, self.owner, objective='website', name='Lunch deal')
        campaign_services.set_targeting(campaign)
        campaign_services.update_draft(campaign, total_budget_cents=50_000)
        campaign_services.add_creative(campaign, headline='h', body='b', cta_label='Go', cta_target='https://luna.example')
        transitions.submit(campaign, self.owner)

        page = self.data(self.client.get(f'/api/v1/commercial/companies/{self.company.pk}/transactions/'))
        self.assertEqual([row['kind'] for row in page['results']], ['campaign_reserve', 'credit_purchase'])
        self.assertEqual(page['results'][0]['campaign_name'], 'Lunch deal')
        self.assertEqual(page['results'][0]['reserved_after_cents'], 50_000)
        self.assertEqual(
            self.client.get(f'/api/v1/commercial/companies/{self.strangers_company.pk}/transactions/').status_code,
            status.HTTP_404_NOT_FOUND,
        )

        company = self.data(self.client.get(f'/api/v1/commercial/companies/{self.company.pk}/'))
        self.assertEqual((company['balance_cents'], company['reserved_cents']), (30_000, 50_000))

        detail = self.data(self.client.get(f'/api/v1/commercial/campaigns/{campaign.pk}/'))
        self.assertEqual((detail['cpm_cents'], detail['reserved_cents'], detail['deletable']), (5_000, 50_000, False))

    def test_budget_increase_over_the_api(self):
        fund(self.company, 100_000)
        campaign = Campaign.objects.create(
            company=self.company, name='Running', objective='website', total_budget_cents=50_000,
            status='running', reserved_cents=0,
        )
        url = f'/api/v1/commercial/campaigns/{campaign.pk}/budget/'
        raised = self.data(self.client.post(url, {'total_budget_cents': 70_000}, format='json'))
        self.assertEqual(raised['total_budget_cents'], 70_000)
        self.assertEqual(
            self.client.post(url, {'total_budget_cents': 60_000}, format='json').status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        Campaign.objects.filter(pk=campaign.pk).update(status='ended')
        self.assertEqual(
            self.client.post(url, {'total_budget_cents': 90_000}, format='json').status_code,
            status.HTTP_409_CONFLICT,
        )

    def test_without_commercial_access_there_is_no_money_either(self):
        plain = User.objects.create_user(username='plain', password='pw')
        self.client.force_authenticate(user=plain)
        for response in (
            self.client.get(f'/api/v1/commercial/pricing/?company={self.company.pk}'),
            self.client.get(f'/api/v1/commercial/companies/{self.company.pk}/transactions/'),
            self.checkout(),
        ):
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# --- slice 2: review --------------------------------------------------------------


@SMALL_FLOORS
class ReviewFindingsTests(TestCase):
    """services/review.py: what review checks before an admin decides."""

    def setUp(self):
        from commercial.services import review

        self.review = review
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner, website='https://luna.example')
        fund(self.company)
        make_group('a', 5)
        make_group('b', 5)

    def submitted(self, **creative):
        campaign = campaign_services.create_draft(
            self.company, self.owner, objective='website', name='Lunch deal'
        )
        campaign_services.set_targeting(campaign)
        campaign_services.update_draft(campaign, total_budget_cents=50_000)
        creative.setdefault('headline', 'Lunch for two')
        creative.setdefault('body', 'Half price until 2pm.')
        creative.setdefault('cta_target', 'https://luna.example/menu')
        campaign_services.add_creative(campaign, cta_label='Go', **creative)
        return transitions.submit(campaign, self.owner)

    def codes(self, campaign):
        return {(f.code, f.severity) for f in self.review.findings(campaign)}

    def test_a_clean_campaign_has_nothing_to_report(self):
        self.assertEqual(self.codes(self.submitted()), set())

    def test_numbers_and_addresses_in_the_text_are_flagged(self):
        campaign = self.submitted(body='Call 0917 123 4567 or see www.luna.example')
        self.assertEqual(
            self.codes(campaign),
            {('phone_in_text', 'warning'), ('link_in_text', 'warning')},
        )

    def test_a_link_away_from_the_companys_website_is_flagged(self):
        campaign = self.submitted(cta_target='https://elsewhere.example/deal')
        self.assertEqual(self.codes(campaign), {('unfamiliar_link', 'warning')})
        # A subdomain of the company's own site is its own.
        campaign = self.submitted(cta_target='https://order.luna.example/')
        self.assertEqual(self.codes(campaign), set())

    def test_blockers(self):
        campaign = self.submitted()
        Company.objects.filter(pk=self.company.pk).update(status=Company.Status.SUSPENDED)
        Campaign.objects.filter(pk=campaign.pk).update(reserved_cents=0)
        campaign.targeting.categories.set([category('school')])
        campaign.refresh_from_db()

        found = self.review.findings(campaign)
        self.assertEqual(
            {f.code for f in self.review.blockers(found)},
            {'company_suspended', 'not_funded', 'audience_too_small'},
        )
        self.assertEqual(found[0].severity, 'blocker')


# --- slice 4: dates, pause, resume, cancel; which groups may carry ads -----------


@SMALL_FLOORS
class LifecycleTests(TestCase):
    """Section 8's remaining arrows: the scheduler's start and end, and the
    owner's pause, resume and cancel - each one event, the money back on the
    two that finish a campaign."""

    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.reviewer = User.objects.create_user(username='reviewer', password='pw')
        self.company = make_company(self.owner)
        fund(self.company, 200_000)
        make_group('a', 5)
        make_group('b', 5)

    def approved(self, name='Lunch deal', budget_cents=50_000):
        campaign = campaign_services.create_draft(
            self.company, self.owner, objective=Campaign.Objective.WEBSITE, name=name
        )
        campaign_services.set_targeting(campaign)
        campaign_services.update_draft(campaign, total_budget_cents=budget_cents)
        campaign_services.add_creative(
            campaign, headline='h', body='b', cta_label='Go', cta_target='https://luna.example'
        )
        transitions.submit(campaign, self.owner)
        return transitions.approve(campaign, self.reviewer)

    def dated(self, campaign, **dates):
        Campaign.objects.filter(pk=campaign.pk).update(**dates)
        campaign.refresh_from_db()
        return campaign

    def running(self, **dates):
        campaign = self.dated(self.approved(), **dates)
        return transitions.start(campaign)

    def balance(self):
        self.company.refresh_from_db()
        return self.company.balance_cents, self.company.reserved_cents

    def kinds(self, campaign):
        return list(campaign.events.values_list('kind', flat=True))

    # --- the scheduler -----------------------------------------------------------

    def test_no_start_date_starts_on_the_next_run_after_approval(self):
        campaign = self.approved()
        self.assertEqual(schedule.run(), {'ended': 0, 'started': 1})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'running')
        started = campaign.events.get(kind='started')
        self.assertEqual((started.actor, started.actor_label), (None, ''))

    def test_a_start_date_waits_for_its_day(self):
        campaign = self.dated(self.approved(), starts_at=timezone.now() + timedelta(days=1))
        self.assertEqual(schedule.run(), {'ended': 0, 'started': 0})
        with self.assertRaises(transitions.TransitionRefused):
            transitions.start(campaign)

        self.dated(campaign, starts_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(schedule.run()['started'], 1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'running')

    def test_the_end_date_ends_it_and_returns_what_is_left(self):
        campaign = self.running()
        self.assertEqual(self.balance(), (150_000, 50_000))
        self.dated(campaign, ends_at=timezone.now() - timedelta(minutes=1))

        self.assertEqual(schedule.run(), {'ended': 1, 'started': 0})
        campaign.refresh_from_db()
        self.assertEqual((campaign.status, campaign.reserved_cents), ('ended', 0))
        self.assertEqual(self.balance(), (200_000, 0))
        ended = campaign.events.get(kind='ended')
        self.assertEqual(ended.metadata, {'from': 'running', 'released_cents': 50_000})
        self.assertIsNone(ended.actor)
        row = AdTransaction.objects.get(kind='campaign_release', campaign=campaign)
        self.assertEqual((row.amount_cents, row.reserved_change_cents), (50_000, -50_000))

    def test_approved_after_its_end_date_it_ends_without_ever_starting(self):
        campaign = self.dated(
            self.approved(),
            starts_at=timezone.now() - timedelta(days=2),
            ends_at=timezone.now() - timedelta(days=1),
        )
        with self.assertRaises(transitions.TransitionRefused):
            transitions.start(campaign)
        self.assertEqual(schedule.run(), {'ended': 1, 'started': 0})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'ended')
        self.assertNotIn('started', self.kinds(campaign))
        self.assertEqual(self.balance(), (200_000, 0))

    def test_a_second_run_finds_nothing_to_do(self):
        self.approved('First')
        self.dated(self.running(), ends_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(schedule.run(), {'ended': 1, 'started': 1})
        self.assertEqual(schedule.run(), {'ended': 0, 'started': 0})

    def test_a_campaign_that_cannot_move_is_skipped_and_the_rest_still_move(self):
        self.approved('First')
        self.approved('Second')
        real_start = transitions.start
        calls = []

        def start_once_refused(campaign):
            calls.append(campaign.pk)
            if len(calls) == 1:
                raise transitions.TransitionRefused('moved meanwhile')
            return real_start(campaign)

        with mock.patch.object(transitions, 'start', start_once_refused):
            self.assertEqual(schedule.run(), {'ended': 0, 'started': 1})

    def test_the_task_runs_the_schedule(self):
        from commercial.tasks import run_campaign_schedule

        self.approved()
        self.assertEqual(run_campaign_schedule(), {'ended': 0, 'started': 1})

    # --- the owner ---------------------------------------------------------------

    def test_pause_and_resume(self):
        campaign = self.running()
        campaign = transitions.pause(campaign, self.owner)
        self.assertEqual(campaign.status, 'paused')
        with self.assertRaises(transitions.TransitionRefused):
            transitions.pause(campaign, self.owner)

        campaign = transitions.resume(campaign, self.owner)
        self.assertEqual(campaign.status, 'running')
        with self.assertRaises(transitions.TransitionRefused):
            transitions.resume(campaign, self.owner)

        self.assertEqual(self.kinds(campaign)[-3:], ['started', 'paused', 'resumed'])
        self.assertEqual(campaign.events.get(kind='paused').actor, self.owner)
        # Pausing keeps the money for when it resumes.
        self.assertEqual(self.balance(), (150_000, 50_000))

    def test_resume_is_refused_past_the_end_date_or_with_nothing_left_to_spend(self):
        campaign = transitions.pause(self.running(), self.owner)
        self.dated(campaign, ends_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(transitions.TransitionRefused):
            transitions.resume(campaign, self.owner)

        self.dated(campaign, ends_at=None, reserved_cents=0)
        with self.assertRaises(transitions.TransitionRefused) as caught:
            transitions.resume(campaign, self.owner)
        self.assertIn('Add budget', str(caught.exception))

    def test_cancel_returns_the_money_from_approved_running_or_paused(self):
        approved = self.approved('Approved')
        running = self.running()
        paused = transitions.pause(self.running(), self.owner)
        self.assertEqual(self.balance(), (50_000, 150_000))

        for campaign, was in ((approved, 'approved'), (running, 'running'), (paused, 'paused')):
            campaign = transitions.cancel(campaign, self.owner)
            self.assertEqual((campaign.status, campaign.reserved_cents), ('ended', 0))
            cancelled = campaign.events.get(kind='cancelled')
            self.assertEqual(cancelled.metadata, {'from': was, 'released_cents': 50_000})
            self.assertEqual(cancelled.actor, self.owner)
        self.assertEqual(self.balance(), (200_000, 0))

    def test_cancel_is_refused_before_review_ends_and_after_it_is_over(self):
        draft = campaign_services.create_draft(self.company, self.owner, objective='website')
        with self.assertRaises(transitions.TransitionRefused):
            transitions.cancel(draft, self.owner)

        campaign = transitions.cancel(self.approved(), self.owner)
        with self.assertRaises(transitions.TransitionRefused):
            transitions.cancel(campaign, self.owner)
        with self.assertRaises(transitions.TransitionRefused):
            transitions.end(campaign)
        self.assertEqual(self.kinds(campaign).count('cancelled'), 1)

    def test_end_is_only_for_a_campaign_whose_end_date_has_passed(self):
        campaign = self.running(ends_at=timezone.now() + timedelta(days=1))
        with self.assertRaises(transitions.TransitionRefused):
            transitions.end(campaign)
        campaign = self.running()  # no end date: runs until it is spent
        with self.assertRaises(transitions.TransitionRefused):
            transitions.end(campaign)


@SMALL_FLOORS
class LifecycleApiTests(APITestCase):
    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner, 'Luna Cafe')
        fund(self.company, 100_000)
        make_group('a', 5)
        make_group('b', 5)
        campaign = campaign_services.create_draft(
            self.company, self.owner, objective='website', name='Lunch deal'
        )
        campaign_services.set_targeting(campaign)
        campaign_services.update_draft(campaign, total_budget_cents=50_000)
        campaign_services.add_creative(
            campaign, headline='h', body='b', cta_label='Go', cta_target='https://luna.example'
        )
        transitions.submit(campaign, self.owner)
        self.campaign = transitions.approve(campaign, self.owner)
        self.client.force_authenticate(user=self.owner)

    def post(self, action, campaign=None):
        return self.client.post(
            f'/api/v1/commercial/campaigns/{(campaign or self.campaign).pk}/{action}/'
        )

    def test_pause_resume_and_cancel(self):
        self.assertEqual(self.post('pause').status_code, status.HTTP_409_CONFLICT)  # not running yet

        transitions.start(self.campaign)
        body = self.post('pause').json()
        self.assertEqual((body['message'], body['data']['status']), ('Campaign paused.', 'paused'))
        self.assertEqual(self.post('resume').json()['data']['status'], 'running')

        data = self.post('cancel').json()['data']
        self.assertEqual((data['status'], data['reserved_cents']), ('ended', 0))
        self.assertEqual(data['events'][-1]['kind'], 'cancelled')
        self.assertEqual(self.post('cancel').status_code, status.HTTP_409_CONFLICT)

    def test_somebody_elses_campaign_is_not_found(self):
        stranger = make_commercial_user('someone-else')
        self.client.force_authenticate(user=stranger)
        for action in ('pause', 'resume', 'cancel'):
            self.assertEqual(self.post(action).status_code, status.HTTP_404_NOT_FOUND)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, 'approved')


@SMALL_FLOORS
class AdFreePlanTests(TestCase):
    """Plan.shows_ads: a group carries ads only when its owner's plan does."""

    def setUp(self):
        self.company = make_company(make_commercial_user('maria'))

    def put_on(self, user, code):
        from billing.models import Plan, Subscription

        Subscription.objects.update_or_create(
            user=user, defaults={'plan': Plan.objects.get(code=code)}
        )

    def test_free_shows_ads_and_the_paid_plans_do_not(self):
        from billing.models import Plan

        self.assertEqual(
            dict(Plan.objects.values_list('code', 'shows_ads')),
            {'free': True, 'premium': False, 'business': False, 'enterprise': False},
        )

    def test_a_group_whose_owner_pays_for_ad_free_is_never_counted(self):
        free = make_group('free', 5)
        no_row = make_group('no-row', 5)  # nobody has subscribed: Free
        paid = make_group('paid', 5)
        self.put_on(free.created_by, 'free')
        self.put_on(paid.created_by, 'premium')

        covered = set(reach.audience_groups('PH').values_list('name', flat=True))
        self.assertEqual(covered, {'free', 'no-row'})

    def test_with_free_made_ad_free_nobody_without_a_subscription_is_counted_either(self):
        from billing.models import Plan

        make_group('no-row', 5)
        Plan.objects.filter(code='free').update(shows_ads=False)
        self.assertFalse(reach.audience_groups('PH').exists())


@SMALL_FLOORS
class GroupMatchTests(TestCase):
    """services/eligibility.py: each rule named, and the verdict always reach's."""

    def setUp(self):
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner)
        self.campaign = campaign_services.create_draft(self.company, self.owner, objective='website')

    def explain(self, group, **audience):
        campaign_services.set_targeting(self.campaign, **audience)
        self.campaign.refresh_from_db()
        result = eligibility.explain(self.campaign, group)
        failed = {check.code for check in result['checks'] if not check.passed}
        return result['matches'], failed

    def test_a_group_that_fits_passes_every_check(self):
        group = make_group('school', 5, category=category('school'), region='PH-00')
        self.assertEqual(self.explain(group, categories=[category('school')]), (True, set()))

    def test_each_rule_that_fails_is_named(self):
        small = make_group('small', 2)
        self.assertEqual(self.explain(small), (False, {'size_floor'}))

        family = make_group('family', 5, category=category('family-friends'), region='PH-07')
        self.assertEqual(
            self.explain(family, categories=[category('school')], regions=['PH-00']),
            (False, {'category', 'place'}),
        )
        self.assertEqual(
            self.explain(family, max_group_size=4), (False, {'size'})
        )

    def test_where_an_undeclared_group_is_comes_from_its_creators_phone(self):
        here = make_group('here', 5)
        abroad = make_group('abroad', 5, country='US')
        unknown = make_group('unknown', 5, country=None)
        self.assertEqual(self.explain(here), (True, set()))
        self.assertEqual(self.explain(abroad), (False, {'place'}))
        self.assertEqual(self.explain(unknown), (False, {'place'}))

    def test_an_ad_free_owner_is_named(self):
        from billing.models import Plan, Subscription

        group = make_group('paid', 5)
        Subscription.objects.create(user=group.created_by, plan=Plan.objects.get(code='premium'))
        matches, failed = self.explain(group)
        self.assertEqual((matches, failed), (False, {'allows_ads'}))

    def test_the_verdict_agrees_with_the_checks_for_every_group(self):
        groups = [
            make_group('one', 5, category=category('school'), region='PH-00'),
            make_group('two', 3, category=category('community')),
            make_group('three', 8, region='US-CA', country='US'),
            make_group('four', 2, region='PH-00'),
        ]
        audiences = [
            {},
            {'categories': [category('school')]},
            {'regions': ['PH-00'], 'min_group_size': 4},
            {'max_group_size': 3},
        ]
        for audience in audiences:
            for group in groups:
                matches, failed = self.explain(group, **audience)
                self.assertEqual(matches, not failed, (group.name, audience, failed))
