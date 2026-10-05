"""Everything a signed-in user hits directly: plans, checkout, portal, wallet."""
from datetime import timedelta
import logging

import stripe
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import services
from billing.models import CreditPackage, Payment, Plan, WalletTransaction
from billing.pagination import TransactionPagination
from billing.serializers import (
    BillingPortalRequestSerializer,
    CreditCheckoutRequestSerializer,
    CreditPackageSerializer,
    PaymentSerializer,
    PlanCheckoutRequestSerializer,
    PlanSerializer,
    SubscriptionSerializer,
    TransactionHistoryQuerySerializer,
    WalletSerializer,
)
from billing.throttles import BillingPortalRateThrottle, CheckoutRateThrottle
from chat.models import Conversation, Message, Participant

logger = logging.getLogger(__name__)


class PlanListView(APIView):
    """The pricing page's data - public, so a logged-out visitor can see it.

    Excludes every custom-priced row, not just the seeded Enterprise
    placeholder: `enterprise.services._sync_account_plan` mints one
    `Plan(code=<contract_number>, is_custom_pricing=True)` per Enterprise
    contract purely so `Subscription.plan` has somewhere to point that
    carries that contract's limits - those are per-customer bookkeeping,
    never something to advertise on the public pricing page. Enterprise
    itself is quoted through the Plan Builder (`enterprise` app), not
    self-serve checkout.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        plans = Plan.objects.filter(active=True, is_custom_pricing=False)
        return Response(PlanSerializer(plans, many=True).data)


class CreditPackageListView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        packages = CreditPackage.objects.filter(active=True)
        return Response(CreditPackageSerializer(packages, many=True).data)


class BillingSummaryView(APIView):
    """What the caller is on right now: their plan, their wallet balance, and
    (if they have one) their Enterprise contract's message-credit balance."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription = services.get_or_create_subscription(request.user)
        wallet = services.get_or_create_wallet(request.user)
        return Response({
            'subscription': SubscriptionSerializer(subscription).data,
            'wallet': WalletSerializer(wallet).data,
            'enterprise_credits': self._enterprise_credits(request.user),
        })

    def _enterprise_credits(self, user):
        # Deferred: `enterprise.services` imports `billing.services` at
        # module level, so importing it back at this module's top level
        # would be circular. Kept separate from `wallet` - a message-count
        # allowance bundled into an Enterprise subscription, not the cents-
        # denominated PAYG SMS wallet - rather than converted into one.
        from enterprise import services as enterprise_services

        contract = enterprise_services.get_current_contract(user)
        if contract is None or contract.status not in ('active', 'past_due'):
            return None

        return {
            'contract_number': contract.contract_number,
            'status': contract.status,
            'balance': enterprise_services.current_balance(contract),
            'monthly_allowance': contract.configuration_snapshot['credits']['monthly_messages'],
        }


class BillingAnalyticsView(APIView):
    """Feature-gated analytics snapshot for the signed-in user."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription = services.get_or_create_subscription(request.user)
        plan = subscription.plan
        features = set((feature or '').strip().lower() for feature in (plan.features or []))

        has_advanced = 'advanced analytics' in features
        has_basic = has_advanced or ('basic analytics' in features)

        payload = {
            'access': {
                'has_basic_analytics': has_basic,
                'has_advanced_analytics': has_advanced,
                'current_plan_code': plan.code,
                'matched_feature': 'advanced' if has_advanced else ('basic' if has_basic else 'none'),
            },
            'basic': None,
            'advanced': None,
            'suggestions': self._suggestions(level='none'),
        }

        if has_advanced:
            payload['basic'] = self._basic_data(request.user)
            payload['advanced'] = self._advanced_data(request.user)
            payload['suggestions'] = self._suggestions(level='advanced')
        elif has_basic:
            payload['basic'] = self._basic_data(request.user)
            payload['suggestions'] = self._suggestions(level='basic')

        return Response(payload)

    def _basic_data(self, user):
        since = timezone.now() - timedelta(days=30)
        subscription = services.get_or_create_subscription(user)
        plan = subscription.plan
        wallet = services.get_or_create_wallet(user)
        active_memberships = Participant.objects.filter(
            user=user,
            status=Participant.Status.ACTIVE,
        )
        conversation_ids = active_memberships.values_list('conversation_id', flat=True)

        conversations = Conversation.objects.filter(id__in=conversation_ids)
        total_conversations = conversations.count()
        group_conversations = conversations.filter(kind=Conversation.Kind.GROUP).count()
        direct_conversations = conversations.filter(kind=Conversation.Kind.DIRECT).count()

        sent_30d = Message.objects.filter(sender=user, created_at__gte=since).count()
        received_30d = Message.objects.filter(
            conversation_id__in=conversation_ids,
            created_at__gte=since,
        ).exclude(sender=user).count()

        purchase_30d = WalletTransaction.objects.filter(
            wallet=wallet,
            created_at__gte=since,
            kind=WalletTransaction.Kind.CREDIT_PURCHASE,
        )
        usage_30d = WalletTransaction.objects.filter(
            wallet=wallet,
            created_at__gte=since,
            kind=WalletTransaction.Kind.USAGE_DEBIT,
        )

        purchased_30d_cents = sum(purchase_30d.values_list('amount_cents', flat=True))
        used_30d_cents = abs(sum(usage_30d.values_list('amount_cents', flat=True)))

        return {
            'window_days': 30,
            'plan_info': {
                'code': plan.code,
                'name': plan.name,
                'monthly_price_cents': plan.monthly_price_cents,
                'is_custom_pricing': plan.is_custom_pricing,
                'features': plan.features or [],
            },
            'wallet': {
                'balance_cents': wallet.balance_cents,
                'usage_last_30_days': {
                    'purchased_cents': purchased_30d_cents,
                    'used_cents': used_30d_cents,
                    'net_change_cents': purchased_30d_cents - used_30d_cents,
                    'purchase_transactions': purchase_30d.count(),
                    'usage_transactions': usage_30d.count(),
                },
            },
            'conversation_counts': {
                'total': total_conversations,
                'group': group_conversations,
                'direct': direct_conversations,
            },
            'message_activity': {
                'sent_last_30_days': sent_30d,
                'received_last_30_days': received_30d,
            },
        }

    def _advanced_data(self, user):
        since = timezone.now() - timedelta(days=30)
        subscription = services.get_or_create_subscription(user)
        plan = subscription.plan
        wallet = services.get_or_create_wallet(user)

        active_memberships = Participant.objects.filter(
            user=user,
            status=Participant.Status.ACTIVE,
        )
        group_ids = active_memberships.filter(
            conversation__kind=Conversation.Kind.GROUP,
        ).values_list('conversation_id', flat=True)

        admin_group_ids = Participant.objects.filter(
            user=user,
            status=Participant.Status.ACTIVE,
            role=Participant.Role.ADMIN,
            conversation__kind=Conversation.Kind.GROUP,
        ).values_list('conversation_id', flat=True)

        groups = Conversation.objects.filter(id__in=group_ids)
        member_counts = groups.annotate(
            active_member_count=Count(
                'participants',
                filter=Q(participants__status=Participant.Status.ACTIVE),
            )
        )
        member_count_values = list(member_counts.values_list('active_member_count', flat=True))
        avg_members = round(sum(member_count_values) / len(member_count_values), 2) if member_count_values else 0.0

        broadcast_groups = groups.filter(mode=Conversation.Mode.BROADCAST).count()

        purchase_30d = WalletTransaction.objects.filter(
            wallet=wallet,
            created_at__gte=since,
            kind=WalletTransaction.Kind.CREDIT_PURCHASE,
        )
        usage_30d = WalletTransaction.objects.filter(
            wallet=wallet,
            created_at__gte=since,
            kind=WalletTransaction.Kind.USAGE_DEBIT,
        )

        purchased_30d_cents = sum(purchase_30d.values_list('amount_cents', flat=True))
        used_30d_cents = abs(sum(usage_30d.values_list('amount_cents', flat=True)))

        return {
            'plan_info': {
                'code': plan.code,
                'name': plan.name,
                'monthly_price_cents': plan.monthly_price_cents,
                'is_custom_pricing': plan.is_custom_pricing,
                'features': plan.features or [],
            },
            'wallet': {
                'balance_cents': wallet.balance_cents,
                'usage_last_30_days': {
                    'purchased_cents': purchased_30d_cents,
                    'used_cents': used_30d_cents,
                    'net_change_cents': purchased_30d_cents - used_30d_cents,
                    'purchase_transactions': purchase_30d.count(),
                    'usage_transactions': usage_30d.count(),
                },
            },
            'group_admin_stats': {
                'admin_groups': admin_group_ids.distinct().count(),
                'member_groups': group_ids.distinct().count(),
                'broadcast_groups': broadcast_groups,
            },
            'group_size': {
                'average_active_members': avg_members,
                'largest_group_active_members': max(member_count_values) if member_count_values else 0,
            },
        }

    def _suggestions(self, *, level):
        base = [
            {
                'code': 'billing-webhook-observability',
                'title': 'Track Stripe webhook outcomes and retries',
                'detail': 'Add dashboard metrics for successful, duplicate, and failed webhook events to catch billing drift early.',
            },
            {
                'code': 'analytics-materialization',
                'title': 'Cache analytics snapshots',
                'detail': 'Materialize analytics in a periodic task if this endpoint becomes hot to avoid expensive live aggregates.',
            },
        ]
        if level == 'none':
            return base + [
                {
                    'code': 'upgrade-analytics-plan',
                    'title': 'Upgrade for richer analytics',
                    'detail': 'Enable a plan with Basic Analytics or Advanced Analytics to unlock usage insights.',
                }
            ]
        if level == 'basic':
            return base + [
                {
                    'code': 'advanced-analytics-upgrade',
                    'title': 'Enable Advanced Analytics',
                    'detail': 'Advanced analytics adds admin-group and group-size signals for deeper engagement analysis.',
                }
            ]
        return base + [
            {
                'code': 'experimentation',
                'title': 'Run feature experiments',
                'detail': 'Use advanced metrics to A/B test onboarding, group settings defaults, and messaging prompts.',
            }
        ]


class TransactionHistoryView(APIView):
    """What the caller has paid over a window, newest first, twenty a page:
    plan and Enterprise plan payments, and credit top-ups.

    `?range=7d|15d|30d` (default 30d) counts back from now. `?range=custom`
    takes `start_date` and `end_date` (YYYY-MM-DD) instead, both whole days in
    the server's timezone. `?kind=` narrows to one kind of payment.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = TransactionHistoryQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        rows = Payment.objects.filter(user=request.user).order_by('-paid_at', '-pk')

        if params['range'] == 'custom':
            # __date compares in the current timezone, so both ends are whole
            # days as the caller wrote them.
            rows = rows.filter(
                paid_at__date__gte=params['start_date'],
                paid_at__date__lte=params['end_date'],
            )
        else:
            days = TransactionHistoryQuerySerializer.RANGE_DAYS[params['range']]
            rows = rows.filter(paid_at__gte=timezone.now() - timedelta(days=days))

        if 'kind' in params:
            rows = rows.filter(kind=params['kind'])

        paginator = TransactionPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(PaymentSerializer(page, many=True).data)


def _stripe_error_response(exc):
    logger.warning('Stripe request failed: %s', exc)
    return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class PlanCheckoutSessionView(APIView):
    """Start a Checkout Session that subscribes the caller to a paid plan."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [CheckoutRateThrottle]

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
    throttle_classes = [CheckoutRateThrottle]

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
    throttle_classes = [BillingPortalRateThrottle]

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
