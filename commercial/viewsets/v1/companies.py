"""Companies, their money, and the lists "Set up your company" picks from.

Thin: the rules are in commercial/services/ (companies, payments). Every
company lookup starts from `Company.objects.manageable_by(request.user)`, so
somebody else's company is a 404.
"""
import logging

import stripe
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.throttles import CheckoutRateThrottle
from commercial.models import Company, Industry
from commercial.pagination import CompanyPagination, TransactionPagination
from commercial.permissions import CanUseCommercial
from commercial.serializers import (
    CheckoutSerializer,
    CompanySerializer,
    IndustrySerializer,
    TransactionSerializer,
    market_options,
)
from commercial.services import companies as services
from commercial.services import payments
from commercial.viewsets.v1.refusals import service_errors

logger = logging.getLogger(__name__)


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
        'checkout': 'Checkout started.',
        'transactions': 'Transactions fetched.',
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

    @action(
        detail=True,
        methods=['post'],
        parser_classes=[JSONParser],
        throttle_classes=[CheckoutRateThrottle],
    )
    def checkout(self, request, pk=None):
        """Start a Stripe Checkout that adds funds to this company's balance.

        Answers with the page to send the person to. The balance moves only
        when Stripe's webhook says the payment cleared, never on the return.
        """
        company = self.get_object()
        body = CheckoutSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            with service_errors():
                session = payments.create_funds_checkout(company, **body.validated_data)
        except stripe.StripeError as exc:
            # billing's answer to the same failure: Stripe, not the caller.
            logger.warning('Stripe request failed: %s', exc)
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'checkout_url': session['url']})

    @action(detail=True, methods=['get'], pagination_class=TransactionPagination)
    def transactions(self, request, pk=None):
        """The company's ledger, newest first, twenty a page."""
        company = self.get_object()
        rows = company.transactions.select_related('campaign').order_by('-created_at', '-pk')
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(TransactionSerializer(page, many=True).data)


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
