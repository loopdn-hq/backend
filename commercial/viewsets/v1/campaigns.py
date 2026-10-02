"""Campaigns: the builder's draft, its audience and creatives, and submitting it.

Thin: the rules are in commercial/services/ (campaigns, transitions, reach).
Every campaign lookup starts from `Campaign.objects.manageable_by(request.user)`,
so somebody else's campaign is a 404, and every company a request names is
looked up through `Company.objects.manageable_by` the same way. Nothing here
reads the stored active company: the request says which company it means.
"""
from django.db.models import Prefetch
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from chat.models import GroupCategory
from commercial.models import Campaign, Creative
from commercial.pagination import CampaignPagination
from commercial.permissions import CanUseCommercial
from commercial.serializers import (
    AudienceSerializer,
    BudgetIncreaseSerializer,
    CampaignCreateSerializer,
    CampaignDetailSerializer,
    CampaignSerializer,
    CreativeSerializer,
    GroupCategorySerializer,
    ReachEstimateSerializer,
)
from commercial.services import campaigns as services
from commercial.services import pricing, reach, transitions
from commercial.viewsets.v1.refusals import manageable_company, service_errors


class CampaignViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    GET    campaigns/?company={id}      the company's campaigns, newest first;
                                        `&status=` one status, `&search=` by name
    POST   campaigns/                   start a draft: {company, objective, name?}
    GET    campaigns/{id}/              with its audience, creatives and history
    PATCH  campaigns/{id}/              save a builder step (drafts only)
    DELETE campaigns/{id}/              delete a draft that never moved money
    PUT    campaigns/{id}/targeting/    save the audience (drafts only)
    GET    campaigns/{id}/creatives/    its creatives
    POST   campaigns/{id}/creatives/    add one (multipart, for the image)
    POST   campaigns/{id}/submit/       draft -> pending review
    POST   campaigns/{id}/withdraw/     pending review -> draft, its money returned
    POST   campaigns/{id}/budget/       raise a running or paused campaign's budget

    No PUT on a campaign itself: the builder saves a step at a time.
    """

    permission_classes = [IsAuthenticated, CanUseCommercial]
    serializer_class = CampaignSerializer
    pagination_class = CampaignPagination
    parser_classes = [JSONParser]

    success_messages = {
        'list': 'Campaigns fetched.',
        'create': 'Draft created.',
        'retrieve': 'Campaign fetched.',
        'partial_update': 'Draft saved.',
        'destroy': 'Draft deleted.',
        'targeting': 'Audience saved.',
        'creatives:GET': 'Creatives fetched.',
        'creatives:POST': 'Creative added.',
        'submit': 'Submitted for review.',
        'withdraw': 'Withdrawn from review.',
        'budget': 'Budget raised.',
    }

    def get_queryset(self):
        campaigns = Campaign.objects.manageable_by(self.request.user).select_related(
            'company'
        )
        if self.action == 'list':
            campaigns = campaigns.filter(company=self._company_from_query())
            campaigns = self._filtered(campaigns)
        else:
            campaigns = campaigns.select_related('targeting').prefetch_related(
                'targeting__categories',
                Prefetch('creatives', queryset=Creative.objects.order_by('created_at', 'pk')),
                'events',
            )
        return campaigns.order_by('-created_at', '-pk')

    def get_serializer_class(self):
        if self.action == 'list':
            return CampaignSerializer
        return CampaignDetailSerializer

    def create(self, request):
        body = CampaignCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        company = manageable_company(request.user, body.validated_data['company'])
        with service_errors():
            campaign = services.create_draft(
                company,
                request.user,
                objective=body.validated_data['objective'],
                name=body.validated_data.get('name', ''),
            )
        return Response(self._detail(campaign), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        campaign = self.get_object()
        body = CampaignSerializer(campaign, data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        with service_errors():
            services.update_draft(campaign, **body.validated_data)
        return Response(self._detail(campaign))

    def destroy(self, request, pk=None):
        with service_errors():
            services.delete_draft(self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['put'])
    def targeting(self, request, pk=None):
        campaign = self.get_object()
        body = AudienceSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        with service_errors():
            services.set_targeting(campaign, **body.validated_data)
        return Response(self._detail(campaign))

    @action(
        detail=True,
        methods=['get', 'post'],
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def creatives(self, request, pk=None):
        campaign = self.get_object()
        context = self.get_serializer_context()
        if request.method == 'GET':
            return Response(
                CreativeSerializer(
                    campaign.creatives.order_by('created_at', 'pk'), many=True, context=context
                ).data
            )

        body = CreativeSerializer(data=request.data, context=context)
        body.is_valid(raise_exception=True)
        with service_errors():
            creative = services.add_creative(campaign, **body.validated_data)
        return Response(
            CreativeSerializer(creative, context=context).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        with service_errors():
            campaign = transitions.submit(self.get_object(), request.user)
        return Response(self._detail(campaign))

    @action(detail=True, methods=['post'])
    def withdraw(self, request, pk=None):
        with service_errors():
            campaign = transitions.withdraw(self.get_object(), request.user)
        return Response(self._detail(campaign))

    @action(detail=True, methods=['post'])
    def budget(self, request, pk=None):
        body = BudgetIncreaseSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        with service_errors():
            campaign = transitions.increase_budget(
                self.get_object(), request.user, body.validated_data['total_budget_cents']
            )
        return Response(self._detail(campaign))

    def _company_from_query(self):
        raw = self.request.query_params.get('company', '').strip()
        if not raw.isdigit():
            raise ValidationError({'company': 'Say which company, e.g. ?company=12.'})
        return manageable_company(self.request.user, int(raw))

    def _filtered(self, campaigns):
        """The list's two narrowings: one status, and a piece of the name."""
        params = self.request.query_params
        wanted = params.get('status', '').strip()
        if wanted:
            if wanted not in Campaign.Status.values:
                raise ValidationError({'status': f'"{wanted}" is not a campaign status.'})
            campaigns = campaigns.filter(status=wanted)
        search = params.get('search', '').strip()
        if search:
            campaigns = campaigns.filter(name__icontains=search)
        return campaigns

    def _detail(self, campaign):
        """The campaign as it is now, read again with everything prefetched."""
        fresh = self.get_queryset().get(pk=campaign.pk)
        return CampaignDetailSerializer(fresh, context=self.get_serializer_context()).data


class CategoryListView(APIView):
    """The group categories an audience can choose from, in the plan's order."""

    permission_classes = [IsAuthenticated, CanUseCommercial]

    def get(self, request):
        categories = GroupCategory.objects.filter(active=True)
        response = Response(GroupCategorySerializer(categories, many=True).data)
        response.message = 'Categories fetched.'
        return response


class ReachEstimateView(APIView):
    """POST reach-estimate/: how many an audience reaches, as ranges, or "too small".

    Takes the audience in the body rather than a saved campaign, so the
    builder can estimate as the person chooses, before anything is saved.
    """

    permission_classes = [IsAuthenticated, CanUseCommercial]

    def post(self, request):
        body = ReachEstimateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        audience = dict(body.validated_data)
        company = manageable_company(request.user, audience.pop('company'))
        with service_errors():
            audience = reach.clean_audience(company, **audience)
        response = Response(reach.estimate(company.country, **audience))
        response.message = 'Reach estimated.'
        return response


class PricingView(APIView):
    """GET pricing/?company={id}: the price in force for that company's currency.

    For the builder's "about N views for this budget". The price a campaign
    pays is fixed at submit; this is only what it would be now.
    """

    permission_classes = [IsAuthenticated, CanUseCommercial]

    def get(self, request):
        raw = request.query_params.get('company', '').strip()
        if not raw.isdigit():
            raise ValidationError({'company': 'Say which company, e.g. ?company=12.'})
        company = manageable_company(request.user, int(raw))
        rate = pricing.rate_in_force(Campaign.Placement.CHAT, company.currency)
        response = Response(
            None
            if rate is None
            else {
                'placement': rate.placement,
                'currency': rate.currency,
                'cpm_cents': rate.cpm_cents,
                'effective_from': rate.effective_from,
            }
        )
        response.message = 'Pricing fetched.'
        return response
