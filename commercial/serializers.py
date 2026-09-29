"""Shapes for the commercial API. They validate input; commercial/services decides."""
from django.conf import settings
from rest_framework import serializers

from commercial.markets import Country, Currency, DEFAULT_CURRENCY
from commercial.models import Company, Industry
from user.models import phone_number_validator


class IndustrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Industry
        fields = ['slug', 'label']
        read_only_fields = fields


class CompanySerializer(serializers.ModelSerializer):
    """A company as its owner sees it, and as "Set up your company" writes it.

    `industry` is written and read as its slug, so a client never needs the
    table's ids. `logo` is write-only and `logo_url` read-only, like a
    profile's avatar.

    Owner, status and money are read-only here and are never passed to the
    services, so a body that names them changes nothing.
    """

    industry = serializers.SlugRelatedField(
        slug_field='slug', queryset=Industry.objects.filter(active=True)
    )
    industry_label = serializers.CharField(source='industry.label', read_only=True)
    country = serializers.ChoiceField(choices=Country.choices)
    # Optional on create: services.create_company defaults it from the country.
    currency = serializers.ChoiceField(choices=Currency.choices, required=False)
    contact_email = serializers.EmailField()
    website = serializers.URLField(required=False, allow_blank=True)
    # Optional, and filled in after setup, on the company's details.
    contact_phone = serializers.CharField(
        required=False, allow_blank=True, max_length=16
    )
    address = serializers.CharField(required=False, allow_blank=True, max_length=255)
    description = serializers.CharField(
        required=False, allow_blank=True, max_length=500
    )
    logo = serializers.ImageField(write_only=True, required=False, allow_null=True)
    logo_url = serializers.SerializerMethodField()
    currency_locked = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = [
            'id',
            'name',
            'industry',
            'industry_label',
            'country',
            'currency',
            'currency_locked',
            'contact_email',
            'contact_phone',
            'website',
            'address',
            'description',
            'logo',
            'logo_url',
            'status',
            'balance_cents',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'status',
            'balance_cents',
            'created_at',
            'updated_at',
        ]

    def get_logo_url(self, obj):
        if not obj.logo:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.logo.url) if request else obj.logo.url

    def get_currency_locked(self, obj):
        """So the Company page can say why the currency will not change."""
        return obj.money_has_moved

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Enter the name of your company.')
        return value

    def validate_contact_phone(self, value):
        value = value.strip()
        if value:
            phone_number_validator(value)
        return value

    def validate_logo(self, value):
        # The same ceiling as an avatar: a logo is shown small, next to an ad.
        if value and value.size > settings.MAX_AVATAR_UPLOAD_BYTES:
            limit_mb = settings.MAX_AVATAR_UPLOAD_BYTES // (1024 * 1024)
            raise serializers.ValidationError(f'Keep the logo under {limit_mb}MB.')
        return value


class ActiveCompanySerializer(serializers.Serializer):
    """The body of PUT active-company/: which company to switch to."""

    company = serializers.IntegerField(min_value=1)


def market_options():
    """The countries a company can be in, each with the currency it starts with.

    Served rather than written into the setup form, so launching a market is
    a change to commercial/markets.py and nothing else.
    """
    return {
        'countries': [
            {
                'code': country.value,
                'name': country.label,
                'default_currency': DEFAULT_CURRENCY[country].value,
            }
            for country in Country
        ],
        'currencies': [
            {'code': currency.value, 'name': currency.label} for currency in Currency
        ],
    }
