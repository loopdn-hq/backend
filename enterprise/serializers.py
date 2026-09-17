from rest_framework import serializers

from billing.serializers import _validate_redirect_url
from enterprise.models import CreditLedgerEntry


class CreditLedgerEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = CreditLedgerEntry
        fields = (
            'id', 'transaction_type', 'amount', 'balance_after',
            'reference_type', 'reference_id', 'created_at',
        )


class DraftCheckoutRequestSerializer(serializers.Serializer):
    # Same allowlisted-origin validation as billing's own checkout requests -
    # these are handed straight to Stripe, which redirects a real customer
    # there once payment completes.
    success_url = serializers.URLField(validators=[_validate_redirect_url])
    cancel_url = serializers.URLField(validators=[_validate_redirect_url])
