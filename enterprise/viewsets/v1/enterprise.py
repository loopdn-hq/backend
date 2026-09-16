"""HTTP surface for the Enterprise Plan Builder: catalog, drafts, checkout,
billing status and the credit ledger. Thin by design - all pricing,
validation and Stripe orchestration lives in `enterprise.services`."""
import logging

import stripe
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from enterprise import services
from enterprise.models import CreditLedgerEntry, EnterprisePlanDraft
from enterprise.pagination import CreditLedgerPagination
from enterprise.serializers import CreditLedgerEntrySerializer, DraftCheckoutRequestSerializer
from enterprise.throttles import EnterpriseCheckoutRateThrottle

logger = logging.getLogger(__name__)

CATALOG_UNAVAILABLE_DETAIL = 'Enterprise pricing is not currently configured.'
DRAFT_NOT_FOUND_DETAIL = 'No plan configuration was found with that id.'
CATALOG_DRIFTED_DETAIL = (
    'Your saved selections have changed since you last configured this plan. '
    'Please review and save again.'
)


def _stripe_error_response(exc):
    logger.warning('Stripe request failed during enterprise checkout: %s', exc)
    return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class PricingView(APIView):
    """The Plan Builder's catalog - public, no auth needed to configure a plan."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        catalog = services.get_active_catalog()
        if catalog is None:
            return Response({'detail': CATALOG_UNAVAILABLE_DETAIL}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(services.serialize_catalog(catalog))


class DraftListCreateView(APIView):
    """Create or update a plan configuration draft.

    Anonymous-allowed, but authentication is not disabled - a caller sending
    a valid bearer token still resolves to `request.user`, which is what
    lets a signed-in save skip the separate attach step entirely.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        draft_id = request.data.get('draft_id') or None
        configuration = request.data.get('configuration')

        try:
            draft, created = services.save_draft(
                draft_id=draft_id, configuration=configuration, request=request,
            )
        except services.CatalogUnavailable:
            return Response({'detail': CATALOG_UNAVAILABLE_DETAIL}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except services.DraftNotFound:
            raise NotFound(DRAFT_NOT_FOUND_DETAIL)
        except services.DraftAccessDenied as exc:
            raise PermissionDenied(exc.message)
        except services.ConfigurationError as exc:
            raise ValidationError(exc.errors)

        response = Response(
            services.serialize_draft_response(draft),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
        response.message = 'Plan configuration saved.'
        return response


class DraftDetailView(APIView):
    """Owner-scoped read of a single draft - by account, or by the anonymous
    session that created it. The draft_id alone is never enough."""

    permission_classes = [AllowAny]

    def get(self, request, draft_id):
        draft = services.get_owned_draft(draft_id, request)
        if draft is None:
            raise NotFound(DRAFT_NOT_FOUND_DETAIL)
        return Response(services.serialize_draft_response(draft))


class DraftAttachView(APIView):
    """Link an anonymous draft to the now-signed-in caller's account."""

    permission_classes = [IsAuthenticated]

    def post(self, request, draft_id):
        draft = EnterprisePlanDraft.objects.filter(draft_id=draft_id).first()
        if draft is None:
            raise NotFound(DRAFT_NOT_FOUND_DETAIL)

        try:
            draft = services.attach_draft(draft, request.user)
        except services.DraftAccessDenied as exc:
            raise PermissionDenied(exc.message)

        response = Response(services.serialize_draft_response(draft))
        response.message = 'Plan configuration linked to your account.'
        return response


class DraftCheckoutView(APIView):
    """Freeze a draft into a Contract and open a Stripe Checkout Session.

    Takes `success_url`/`cancel_url` in the body, same as billing's own
    checkout endpoints - validated against `BILLING_ALLOWED_REDIRECT_ORIGINS`
    since Stripe will redirect a real, just-paid customer there.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [EnterpriseCheckoutRateThrottle]

    def post(self, request, draft_id):
        serializer = DraftCheckoutRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        draft = EnterprisePlanDraft.objects.filter(draft_id=draft_id, account=request.user).first()
        if draft is None:
            raise NotFound(DRAFT_NOT_FOUND_DETAIL)

        try:
            contract, session, invoice_payload = services.checkout_draft(
                draft, request.user,
                success_url=data['success_url'], cancel_url=data['cancel_url'],
            )
        except services.CatalogUnavailable:
            return Response({'detail': CATALOG_UNAVAILABLE_DETAIL}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except services.ConfigurationError:
            return Response({'detail': CATALOG_DRIFTED_DETAIL}, status=status.HTTP_409_CONFLICT)
        except stripe.StripeError as exc:
            return _stripe_error_response(exc)

        return Response({
            'contract_id': str(contract.pk),
            'contract_number': contract.contract_number,
            'checkout_url': session['url'],
            'invoice': invoice_payload,
        })


class BillingSummaryView(APIView):
    """What the caller's Enterprise contract is on right now, if they have one."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        contract = services.get_current_contract(request.user)
        if contract is None:
            return Response({'contract': None})

        stripe_billing = getattr(contract, 'stripe_billing', None)
        return Response({
            'contract': {
                'contract_id': str(contract.pk),
                'contract_number': contract.contract_number,
                'billing_interval': contract.billing_interval,
                'status': contract.status,
                'started_at': contract.started_at,
                'ended_at': contract.ended_at,
                'created_at': contract.created_at,
                'pricing': contract.pricing_snapshot,
                'stripe_status': stripe_billing.status if stripe_billing else '',
            },
        })


class CreditsView(APIView):
    """Current message-credit balance plus the ledger entries behind it."""

    permission_classes = [IsAuthenticated]
    pagination_class = CreditLedgerPagination

    def get(self, request):
        contract = services.get_current_contract(request.user)
        if contract is None:
            empty_page = {'count': 0, 'next': None, 'previous': None, 'results': []}
            return Response({'balance': 0, 'ledger': empty_page})

        balance = services.current_balance(contract)
        queryset = CreditLedgerEntry.objects.filter(contract=contract)

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = CreditLedgerEntrySerializer(page, many=True)
        ledger = paginator.get_paginated_response(serializer.data).data

        return Response({'balance': balance, 'ledger': ledger})
