"""The commercial app.

Two halves. The schema tests hold the models to what
docs/advertising_integration_plan.md promises of them: the money cannot go
wrong, deleting things cannot erase what was delivered or spent, and ads stay
off until somebody switches them on. The company tests are slice 0 of
docs/campaigns_final_plan.md (section 16): companies, and which one is active.
"""
import io
import shutil
import tempfile

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

from commercial.models import (
    AdImpression,
    AdSettings,
    AdTransaction,
    Campaign,
    CommercialPreference,
    Company,
    Creative,
    Industry,
    TargetingCriteria,
)
from commercial.services import companies as services
from chat.models import Conversation, GroupCategory
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
                kind=AdTransaction.Kind.USAGE_DEBIT,
                amount_cents=-500,
                balance_after_cents=0,
            )
        )

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

    def test_an_account_without_ads_access_gets_nothing(self):
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
        self.assertIn(
            {'code': 'PH', 'name': 'Philippines', 'default_currency': 'PHP'},
            markets['countries'],
        )
        self.assertIn(
            {'code': 'US', 'name': 'United States', 'default_currency': 'USD'},
            markets['countries'],
        )
        self.assertEqual(
            {row['code'] for row in markets['currencies']}, {'PHP', 'USD'}
        )
