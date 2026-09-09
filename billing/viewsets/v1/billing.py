"""Everything a signed-in user hits directly: plans, checkout, portal, wallet."""
import logging

import stripe
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import services
from billing.models import CreditPackage, Plan
from billing.serializers import (
    BillingPortalRequestSerializer,
    CreditCheckoutRequestSerializer,
    CreditPackageSerializer,
    PlanCheckoutRequestSerializer,
    PlanSerializer,
    SubscriptionSerializer,
    WalletSerializer,
)

logger = logging.getLogger(__name__)


class PlanListView(APIView):
    """The pricing page's data - public, so a logged-out visitor can see it."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        plans = Plan.objects.filter(active=True)
        return Response(PlanSerializer(plans, many=True).data)


class CreditPackageListView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        packages = CreditPackage.objects.filter(active=True)
        return Response(CreditPackageSerializer(packages, many=True).data)


class BillingSummaryView(APIView):
    """What the caller is on right now: their plan and their wallet balance."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription = services.get_or_create_subscription(request.user)
        wallet = services.get_or_create_wallet(request.user)
        return Response({
            'subscription': SubscriptionSerializer(subscription).data,
            'wallet': WalletSerializer(wallet).data,
        })


def _stripe_error_response(exc):
    logger.warning('Stripe request failed: %s', exc)
    return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class PlanCheckoutSessionView(APIView):
    """Start a Checkout Session that subscribes the caller to a paid plan."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PlanCheckoutRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        plan = Plan.objects.filter(code=data['plan_code'], active=True).first()
        if plan is None:
            raise ValidationError('Unknown plan.')
        if plan.code == Plan.Code.FREE:
            raise ValidationError('The Free plan does not require checkout.')
        if not plan.stripe_price_id:
            raise ValidationError(
                'This plan is not yet available for self-serve checkout. '
                'Contact sales.'
            )

        try:
            session = services.create_plan_checkout_session(
                request.user, plan, data['success_url'], data['cancel_url']
            )
        except stripe.StripeError as exc:
            return _stripe_error_response(exc)

        return Response({'checkout_url': session['url']})


class CreditCheckoutSessionView(APIView):
    """Start a Checkout Session that tops up the caller's wallet."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CreditCheckoutRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        package = data['package_id']

        if not package.stripe_price_id:
            raise ValidationError('This credit package is not yet available for purchase.')

        try:
            session = services.create_credit_checkout_session(
                request.user, package, data['success_url'], data['cancel_url']
            )
        except stripe.StripeError as exc:
            return _stripe_error_response(exc)

        return Response({'checkout_url': session['url']})


class BillingPortalSessionView(APIView):
    """A link to Stripe's hosted page for managing the subscription/cards."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = BillingPortalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            session = services.create_billing_portal_session(
                request.user, serializer.validated_data['return_url']
            )
        except stripe.StripeError as exc:
            return _stripe_error_response(exc)

        return Response({'portal_url': session['url']})
