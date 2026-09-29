"""Companies, and the lists "Set up your company" picks from.

Thin: the rules are in commercial/services/companies.py. Every company lookup
starts from `Company.objects.manageable_by(request.user)`, so somebody else's
company is a 404.
"""
from rest_framework import mixins, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from commercial.models import Company, Industry
from commercial.pagination import CompanyPagination
from commercial.permissions import CanUseCommercial
from commercial.serializers import CompanySerializer, IndustrySerializer, market_options
from commercial.services import companies as services


class CompanyViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated, CanUseCommercial]
    serializer_class = CompanySerializer
    # Multipart for the logo; JSON for everything else.
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    # Ten a page, by name. `?search=` narrows by name. The Companies dialog
    # scrolls through the pages; the dropdown only ever needs the first.
    pagination_class = CompanyPagination
    # PATCH only. A PUT would have to resend the logo to keep it.
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    success_messages = {
        'list': 'Companies fetched.',
        'create': 'Company created.',
        'retrieve': 'Company fetched.',
        'partial_update': 'Company updated.',
    }

    def get_queryset(self):
        companies = (
            Company.objects.manageable_by(self.request.user)
            .select_related('industry')
            # pk as well as name: two companies with one name must not swap
            # places between pages.
            .order_by('name', 'pk')
        )
        search = self.request.query_params.get('search', '').strip()
        if search and self.action == 'list':
            companies = companies.filter(name__icontains=search)
        return companies

    def perform_create(self, serializer):
        # The owner is the caller. Nothing in the body can name somebody else,
        # and the serializer does not have an owner field to try.
        serializer.instance = services.create_company(
            self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        try:
            serializer.instance = services.update_company(
                serializer.instance, **serializer.validated_data
            )
        except services.CurrencyLocked as exc:
            raise ValidationError({'currency': [exc.message]})


class IndustryListView(APIView):
    """The industries a company can say it is in, in the setup form's order."""

    permission_classes = [IsAuthenticated, CanUseCommercial]

    def get(self, request):
        industries = Industry.objects.filter(active=True)
        response = Response(IndustrySerializer(industries, many=True).data)
        response.message = 'Industries fetched.'
        return response


class MarketListView(APIView):
    """The countries a company can advertise in, and the currencies it can pay in."""

    permission_classes = [IsAuthenticated, CanUseCommercial]

    def get(self, request):
        response = Response(market_options())
        response.message = 'Markets fetched.'
        return response
