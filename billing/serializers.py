from urllib.parse import urlsplit

from django.conf import settings
from rest_framework import serializers

from billing.models import CreditPackage, Plan, Subscription, Wallet


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
            'storage_gb', 'has_whatsapp', 'has_telegram', 'features',
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


class PlanCheckoutRequestSerializer(serializers.Serializer):
    plan_code = serializers.ChoiceField(choices=Plan.Code.choices)
    success_url = serializers.URLField(validators=[_validate_redirect_url])
    cancel_url = serializers.URLField(validators=[_validate_redirect_url])


class CreditCheckoutRequestSerializer(serializers.Serializer):
    package_id = serializers.PrimaryKeyRelatedField(
        queryset=CreditPackage.objects.filter(active=True)
    )
    success_url = serializers.URLField(validators=[_validate_redirect_url])
    cancel_url = serializers.URLField(validators=[_validate_redirect_url])


class BillingPortalRequestSerializer(serializers.Serializer):
    return_url = serializers.URLField(validators=[_validate_redirect_url])
