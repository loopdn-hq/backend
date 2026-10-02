"""The admin site's API. Campaign review is slice 2 of
docs/campaigns_final_plan.md; the rules it calls are tested with the
commercial app (commercial/tests.py), these prove the routes.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from commercial.models import Campaign, Company, Creative
from commercial.services import campaigns as campaign_services
from commercial.services import transitions
from commercial.tests import (
    SMALL_FLOORS,
    fund,
    make_commercial_user,
    make_company,
    make_group,
)
from rbac import services as rbac_services
from rbac.models import UserRole

User = get_user_model()


def make_admin(username='reviewer'):
    user = User.objects.create_user(username=username, password='pw')
    rbac_services.grant(user, UserRole.Role.ADMIN)
    return user


@SMALL_FLOORS
class CampaignReviewApiTests(APITestCase):
    url = '/api/v1/admin/campaigns/'

    def setUp(self):
        cache.clear()
        self.admin = make_admin()
        self.owner = make_commercial_user('maria')
        self.company = make_company(self.owner, 'Luna Cafe')
        fund(self.company, 200_000)
        make_group('a', 5)
        make_group('b', 5)
        self.client.force_authenticate(user=self.admin)

    def draft(self, company=None, owner=None, name='Lunch deal', budget=50_000):
        company, owner = company or self.company, owner or self.owner
        campaign = campaign_services.create_draft(company, owner, objective='website', name=name)
        campaign_services.set_targeting(campaign)
        campaign_services.update_draft(campaign, total_budget_cents=budget)
        campaign_services.add_creative(
            campaign, headline='Lunch for two', body='Half price until 2pm.',
            cta_label='Go', cta_target='https://luna.example/menu',
        )
        return campaign

    def submitted(self, **kwargs):
        campaign = self.draft(**kwargs)
        return transitions.submit(campaign, kwargs.get('owner') or self.owner)

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

    def detail(self, campaign, suffix=''):
        return f'{self.url}{campaign.pk}/{suffix}'

    # --- who gets in -------------------------------------------------------

    def test_only_an_admin_who_may_review_gets_in(self):
        campaign = self.submitted()
        commercial = make_commercial_user('someone')
        plain = User.objects.create_user(username='plain', password='pw')
        for user in (commercial, plain):
            self.client.force_authenticate(user=user)
            self.refused(self.client.get(self.url), status.HTTP_403_FORBIDDEN)
            self.refused(
                self.client.post(self.detail(campaign, 'approve/')), status.HTTP_403_FORBIDDEN
            )

        # The admin site's door alone is not enough: review is its own feature.
        rbac_services.set_grant('platform.access', 'standard', True, by=self.admin)
        cache.clear()
        self.client.force_authenticate(user=plain)
        self.refused(self.client.get(self.url), status.HTTP_403_FORBIDDEN)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.PENDING_REVIEW)

    # --- the queue ----------------------------------------------------------------

    def test_the_queue_is_every_companys_waiting_campaigns_oldest_first(self):
        first = self.submitted(name='First')
        other_owner = make_commercial_user('jo')
        other_company = make_company(other_owner, 'City Fitness')
        fund(other_company, 200_000)
        second = self.submitted(company=other_company, owner=other_owner, name='Second')
        self.draft(name='Not submitted')

        page = self.data(self.client.get(self.url))
        self.assertEqual([row['id'] for row in page['results']], [first.pk, second.pk])
        self.assertEqual(page['results'][1]['company_name'], 'City Fitness')
        self.assertEqual(page['results'][0]['creative_count'], 1)
        self.assertEqual(
            (page['results'][0]['objective_label'], page['results'][0]['status_label']),
            ('Website visits', 'Pending review'),
        )
        self.assertIsNotNone(page['results'][0]['submitted_at'])

    def test_status_and_search_narrow_it(self):
        self.submitted(name='Lunch deal')
        self.draft(name='Dinner deal')

        everything = self.data(self.client.get(f'{self.url}?status=all'))
        self.assertEqual(everything['count'], 2)
        drafts = self.data(self.client.get(f'{self.url}?status=draft'))
        self.assertEqual([row['name'] for row in drafts['results']], ['Dinner deal'])
        by_company = self.data(self.client.get(f'{self.url}?status=all&search=luna'))
        self.assertEqual(by_company['count'], 2)
        self.refused(self.client.get(f'{self.url}?status=nope'), status.HTTP_400_BAD_REQUEST)

    def test_one_campaign_shows_its_company_checks_and_history(self):
        campaign = self.draft()
        Creative.objects.filter(campaign=campaign).update(body='Call 0917 123 4567')
        transitions.submit(campaign, self.owner)

        from commercial.models import TargetingCriteria

        TargetingCriteria.objects.filter(campaign=campaign).update(regions=['PH-10'])

        shown = self.data(self.client.get(self.detail(campaign)))
        self.assertEqual(shown['company']['name'], 'Luna Cafe')
        self.assertEqual(
            shown['targeting'],
            {
                'categories': [],
                'regions': [{'code': 'PH-10', 'name': 'Northern Mindanao'}],
                'min_group_size': None,
                'max_group_size': None,
            },
        )
        self.assertEqual(shown['company']['owner'], 'maria')
        # The audience is now one region no group has declared: too small.
        self.assertEqual(
            [(f['code'], f['severity']) for f in shown['findings']],
            [('audience_too_small', 'blocker'), ('phone_in_text', 'warning')],
        )
        self.assertEqual(shown['events'][-1]['by'], 'maria')
        self.assertEqual(shown['events'][-1]['metadata']['reserved_cents'], 50_000)

    # --- approve ------------------------------------------------------------------

    def test_approve_passes_it_and_its_creatives_and_keeps_the_money_reserved(self):
        campaign = self.submitted()
        approved = self.data(
            self.client.post(self.detail(campaign, 'approve/'), {'note': 'Fine.'}, format='json')
        )
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual({c['status'] for c in approved['creatives']}, {'approved'})
        self.assertEqual(approved['events'][-1]['kind'], 'approved')
        self.assertEqual(approved['events'][-1]['by'], 'reviewer')
        self.assertEqual(approved['reserved_cents'], 50_000)

        # The company sees that Loopd'n decided, not who.
        self.client.force_authenticate(user=self.owner)
        mine = self.data(self.client.get(f'/api/v1/commercial/campaigns/{campaign.pk}/'))
        self.assertEqual(mine['events'][-1]['by'], "Loopd'n")

    def test_a_blocker_stops_approval_and_changes_nothing(self):
        campaign = self.submitted()
        Company.objects.filter(pk=self.company.pk).update(status=Company.Status.SUSPENDED)
        body = self.refused(
            self.client.post(self.detail(campaign, 'approve/')), status.HTTP_409_CONFLICT
        )
        self.assertIn('suspended', body['message'])
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.PENDING_REVIEW)

    # --- reject -------------------------------------------------------------------

    def test_reject_sends_it_back_with_notes_and_returns_the_money(self):
        campaign = self.submitted()
        creative = campaign.creatives.get()

        rejected = self.data(
            self.client.post(
                self.detail(campaign, 'reject/'),
                {'note': 'The offer needs an end date.',
                 'creatives': [{'id': creative.pk, 'note': 'Say until when.'}]},
                format='json',
            )
        )
        self.assertEqual(rejected['status'], 'draft')
        self.assertEqual(rejected['reserved_cents'], 0)
        self.assertEqual(
            (rejected['creatives'][0]['status'], rejected['creatives'][0]['review_notes']),
            ('rejected', 'Say until when.'),
        )
        self.assertEqual(rejected['events'][-1]['note'], 'The offer needs an end date.')
        self.company.refresh_from_db()
        self.assertEqual((self.company.balance_cents, self.company.reserved_cents), (200_000, 0))

        # The company reads the reason in its history.
        self.client.force_authenticate(user=self.owner)
        mine = self.data(self.client.get(f'/api/v1/commercial/campaigns/{campaign.pk}/'))
        self.assertEqual(mine['events'][-1]['note'], 'The offer needs an end date.')

    def test_reject_needs_a_reason_and_this_campaigns_creatives(self):
        campaign = self.submitted()
        other = self.submitted(name='Other')
        for body in (
            {'note': '   '},
            {'note': 'No.', 'creatives': [{'id': other.creatives.get().pk, 'note': 'x'}]},
        ):
            self.refused(
                self.client.post(self.detail(campaign, 'reject/'), body, format='json'),
                status.HTTP_400_BAD_REQUEST,
            )
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.PENDING_REVIEW)

    def test_only_a_waiting_campaign_can_be_decided(self):
        draft = self.draft()
        for action in ('approve/', 'reject/'):
            self.refused(
                self.client.post(self.detail(draft, action), {'note': 'x'}, format='json'),
                status.HTTP_409_CONFLICT,
            )

    def test_resubmitting_starts_the_review_clean(self):
        campaign = self.submitted()
        creative = campaign.creatives.get()
        transitions.reject(campaign, self.admin, 'Fix it.', creative_notes={creative.pk: 'This one.'})

        transitions.submit(campaign, self.owner)
        creative.refresh_from_db()
        self.assertEqual((creative.status, creative.review_notes), ('pending_review', ''))

    # --- why a group matches or not (slice 4) --------------------------------------

    def test_match_names_each_rule_for_one_group(self):
        from commercial.tests import category

        campaign = self.submitted()
        campaign.targeting.categories.set([category('school')])
        school = make_group('school', 5, category=category('school'), region='PH-00')
        family = make_group('family', 5, category=category('family-friends'))

        data = self.data(self.client.get(self.detail(campaign, f'match/?group={school.pk}')))
        self.assertTrue(data['matches'])
        self.assertEqual(data['group'], {'id': school.pk, 'name': 'school', 'member_count': 5})
        self.assertTrue(all(check['passed'] for check in data['checks']))

        data = self.data(self.client.get(self.detail(campaign, f'match/?group={family.pk}')))
        self.assertFalse(data['matches'])
        failed = [check for check in data['checks'] if not check['passed']]
        self.assertEqual([check['code'] for check in failed], ['category'])
        self.assertEqual(failed[0]['detail'], 'It is "Family and friends".')

    def test_match_needs_a_group_that_exists_and_the_review_feature(self):
        campaign = self.submitted()
        self.refused(self.client.get(self.detail(campaign, 'match/')), status.HTTP_400_BAD_REQUEST)
        self.refused(
            self.client.get(self.detail(campaign, 'match/?group=999999')), status.HTTP_404_NOT_FOUND
        )
        group = make_group('school', 5)
        self.client.force_authenticate(user=make_commercial_user('someone'))
        self.refused(
            self.client.get(self.detail(campaign, f'match/?group={group.pk}')),
            status.HTTP_403_FORBIDDEN,
        )
