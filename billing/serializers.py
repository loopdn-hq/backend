from urllib.parse import urlsplit

from django.conf import settings
from rest_framework import serializers

from billing.models import CreditPackage, Plan, Subscription, Wallet, WalletTransaction


def _validate_redirect_url(value):
    """Refuse a redirect target this API does not already trust.

    success_url/cancel_url/return_url are handed straight to Stripe, which
    later redirects a real, just-completed customer there - an unvalidated
    URL here is an open redirect Stripe would carry out on our behalf. Checked
    against the same origins CORS already trusts, not a separate allowlist.
    """
    origin = f'{urlsplit(value).scheme}://{urlsplit(value).netloc}'
    if origin not in settings.BILLING_ALLOWED_REDIRECT_ORIGINS:
        raise serializers.ValidationError(
            'This redirect URL is not on an allowed origin for this app.'
        )
    return value


class PlanSerializer(serializers.ModelSerializer):
    price_display = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = (
            'code', 'name', 'monthly_price_cents', 'price_display',
            'max_groups', 'max_members_per_group', 'max_admins_per_group',
            'storage_gb', 'has_calls', 'max_call_participants',
            'has_screen_share', 'has_call_recording', 'call_recording_retention_days',
            'has_whatsapp', 'has_telegram', 'features',
            'is_custom_pricing',
        )

    def get_price_display(self, obj):
        if obj.is_custom_pricing or obj.monthly_price_cents is None:
            return 'Custom'
        return f'${obj.monthly_price_cents / 100:.2f}/mo'


class CreditPackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = CreditPackage
        fields = ('id', 'name', 'price_cents', 'credit_cents')


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer()

    class Meta:
        model = Subscription
        fields = (
            'plan', 'status', 'current_period_end', 'cancel_at_period_end',
        )


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ('balance_cents', 'auto_recharge_enabled', 'auto_recharge_package')


class WalletTransactionSerializer(serializers.ModelSerializer):
    """One line of the caller's wallet ledger, as their Billing page lists it."""

    kind_label = serializers.CharField(source='get_kind_display', read_only=True)

    class Meta:
        model = WalletTransaction
        fields = (
            'id', 'kind', 'kind_label', 'amount_cents', 'balance_after_cents',
            'description', 'stripe_payment_intent_id', 'created_at',
        )
        read_only_fields = fields


class TransactionHistoryQuerySerializer(serializers.Serializer):
    """The query string of the transaction history: which window, which kind.

    `range` picks a preset window ending now; `custom` takes `start_date` and
    `end_date` instead, both inclusive calendar days.
    """

    RANGE_DAYS = {'7d': 7, '15d': 15, '30d': 30}

    range = serializers.ChoiceField(choices=[*RANGE_DAYS, 'custom'], default='30d')
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    kind = serializers.ChoiceField(choices=WalletTransaction.Kind.choices, required=False)

    def validate(self, attrs):
        if attrs['range'] != 'custom':
            return attrs

        missing = {
            field: 'This field is required when range is custom.'
            for field in ('start_date', 'end_date') if field not in attrs
        }
        if missing:
            raise serializers.ValidationError(missing)
        if attrs['start_date'] > attrs['end_date']:
            raise serializers.ValidationError(
                {'end_date': 'end_date must not be before start_date.'}
            )
        return attrs


class PlanCheckoutRequestSerializer(serializers.Serializer):
    plan_code = serializers.CharField()
    success_url = serializers.URLField(validators=[_validate_redirect_url])
    cancel_url = serializers.URLField(validators=[_validate_redirect_url])

    def validate_plan_code(self, value):
        if not Plan.objects.filter(code=value, active=True).exists():
            raise serializers.ValidationError('This plan is not available for checkout.')
        return value


class CreditCheckoutRequestSerializer(serializers.Serializer):
    package_id = serializers.PrimaryKeyRelatedField(
        queryset=CreditPackage.objects.filter(active=True)
    )
    success_url = serializers.URLField(validators=[_validate_redirect_url])
    cancel_url = serializers.URLField(validators=[_validate_redirect_url])


class BillingPortalRequestSerializer(serializers.Serializer):
    return_url = serializers.URLField(validators=[_validate_redirect_url])
