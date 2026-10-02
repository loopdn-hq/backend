"""Shapes for the commercial API. They validate input; commercial/services decides."""
from django.conf import settings
from rest_framework import serializers

from billing.serializers import _validate_redirect_url
from chat.models import GroupCategory
from commercial.markets import (
    Country,
    Currency,
    DEFAULT_CURRENCY,
    MAX_TOP_UP_CENTS,
    MIN_TOP_UP_CENTS,
    REGIONS,
)
from commercial.models import (
    AdTransaction,
    Campaign,
    CampaignEvent,
    Company,
    Creative,
    Industry,
)
from user.models import phone_number_validator

#: The most a budget field holds: Campaign's columns are 32-bit.
MAX_BUDGET_CENTS = 2_000_000_000


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
            'reserved_cents',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'status',
            'balance_cents',
            'reserved_cents',
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
    """The countries a company can be in, each with the currency it starts with
    and the regions its campaigns can target.

    Served rather than written into the setup form and the builder, so
    launching a market is a change to commercial/markets.py and nothing else.
    """
    return {
        'countries': [
            {
                'code': country.value,
                'name': country.label,
                'default_currency': DEFAULT_CURRENCY[country].value,
                'regions': [
                    {'code': code, 'name': name} for code, name in REGIONS[country]
                ],
            }
            for country in Country
        ],
        'currencies': [
            {
                'code': currency.value,
                'name': currency.label,
                'min_top_up_cents': MIN_TOP_UP_CENTS[currency],
            }
            for currency in Currency
        ],
    }


# -- campaigns -------------------------------------------------------------------


class GroupCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupCategory
        fields = ['slug', 'label']
        read_only_fields = fields


class AudienceSerializer(serializers.Serializer):
    """An audience, as the builder's step 2 saves it and step 3 estimates it.

    Each part may be left empty, meaning "any". Categories are slugs; regions
    are ISO codes, checked against the company's country by the services.
    """

    categories = serializers.SlugRelatedField(
        many=True,
        slug_field='slug',
        queryset=GroupCategory.objects.filter(active=True),
        required=False,
    )
    regions = serializers.ListField(
        child=serializers.CharField(max_length=6), required=False, max_length=100
    )
    min_group_size = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    max_group_size = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class ReachEstimateSerializer(AudienceSerializer):
    """POST reach-estimate/: an audience, for one of the caller's companies."""

    company = serializers.IntegerField(min_value=1)


class TargetingSerializer(serializers.Serializer):
    """A campaign's saved audience, as the builder reads it back."""

    categories = serializers.SlugRelatedField(many=True, slug_field='slug', read_only=True)
    regions = serializers.ListField(child=serializers.CharField(), read_only=True)
    min_group_size = serializers.IntegerField(read_only=True)
    max_group_size = serializers.IntegerField(read_only=True)


class CreativeSerializer(serializers.ModelSerializer):
    """One version of the ad: what it says, shows, and where its button goes.

    `image` is write-only and `image_url` read-only, like a company's logo.
    `cta_kind` may be left out: each objective has one button, and the
    services fill it in. `status` and `review_notes` belong to review.
    """

    image = serializers.ImageField(write_only=True, required=False, allow_null=True)
    image_url = serializers.SerializerMethodField()
    cta_kind = serializers.ChoiceField(choices=Creative.CtaKind.choices, required=False)
    cta_target = serializers.CharField(max_length=500, required=False, allow_blank=True)

    class Meta:
        model = Creative
        fields = [
            'id',
            'campaign',
            'headline',
            'body',
            'image',
            'image_url',
            'cta_label',
            'cta_kind',
            'cta_target',
            'status',
            'review_notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'campaign',
            'status',
            'review_notes',
            'created_at',
            'updated_at',
        ]

    def get_image_url(self, obj):
        if not obj.image:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url

    def _required_text(self, value, message):
        value = value.strip()
        if not value:
            raise serializers.ValidationError(message)
        return value

    def validate_headline(self, value):
        return self._required_text(value, 'Write a headline.')

    def validate_body(self, value):
        return self._required_text(value, 'Write what the ad says.')

    def validate_cta_label(self, value):
        return self._required_text(value, 'Label the button.')

    def validate_image(self, value):
        limit = settings.MAX_CREATIVE_IMAGE_UPLOAD_BYTES
        if value and value.size > limit:
            raise serializers.ValidationError(
                f'Keep the image under {limit // (1024 * 1024)}MB.'
            )
        return value


class CampaignEventSerializer(serializers.ModelSerializer):
    """One line of a campaign's history, as its company sees it."""

    kind_label = serializers.CharField(source='get_kind_display', read_only=True)
    by = serializers.SerializerMethodField()

    #: Done by Loopd'n's reviewers, who are not named to a company.
    REVIEW_KINDS = {CampaignEvent.Kind.APPROVED, CampaignEvent.Kind.REJECTED}

    class Meta:
        model = CampaignEvent
        fields = ['kind', 'kind_label', 'by', 'note', 'created_at']
        read_only_fields = fields

    def get_by(self, obj):
        """Who did it: the person, "Loopd'n" for review, or null for the system."""
        if obj.kind in self.REVIEW_KINDS:
            return "Loopd'n"
        return obj.actor_label or None


class CampaignSerializer(serializers.ModelSerializer):
    """A campaign as the list shows it, and as the builder edits a draft.

    Every field may be empty on a draft; submit is what checks the whole.
    Amounts are minor units of `currency`, the company's, and `country` is the
    company's too - the market its regions are in. The company, status,
    placement and money are read-only: a body that names them changes nothing.
    `cpm_cents` is the price it was submitted at; null on a draft.
    """

    name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    total_budget_cents = serializers.IntegerField(
        min_value=1, max_value=MAX_BUDGET_CENTS, required=False, allow_null=True
    )
    daily_budget_cents = serializers.IntegerField(
        min_value=1, max_value=MAX_BUDGET_CENTS, required=False, allow_null=True
    )
    currency = serializers.CharField(source='company.currency', read_only=True)
    country = serializers.CharField(source='company.country', read_only=True)

    class Meta:
        model = Campaign
        fields = [
            'id',
            'company',
            'name',
            'objective',
            'placement',
            'status',
            'pacing',
            'total_budget_cents',
            'daily_budget_cents',
            'currency',
            'country',
            'cpm_cents',
            'reserved_cents',
            'spent_cents',
            'starts_at',
            'ends_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'company',
            'placement',
            'status',
            'currency',
            'country',
            'cpm_cents',
            'reserved_cents',
            'spent_cents',
            'created_at',
            'updated_at',
        ]


class CampaignDetailSerializer(CampaignSerializer):
    """One campaign with everything the builder and the campaign page show."""

    targeting = serializers.SerializerMethodField()
    creatives = CreativeSerializer(many=True, read_only=True)
    events = CampaignEventSerializer(many=True, read_only=True)
    deletable = serializers.SerializerMethodField()

    class Meta(CampaignSerializer.Meta):
        fields = [
            *CampaignSerializer.Meta.fields,
            'targeting',
            'creatives',
            'events',
            'deletable',
        ]
        read_only_fields = fields

    def get_deletable(self, obj):
        """A draft that never moved money - so the page offers Delete only
        where it would work. A withdrawn campaign keeps its ledger rows.
        """
        return obj.status == Campaign.Status.DRAFT and not obj.transactions.exists()

    def get_targeting(self, obj):
        """Null until the audience step has been saved: "not chosen yet" is not
        the same as "any", which is a saved audience with nothing narrowed.
        """
        try:
            targeting = obj.targeting
        except Campaign.targeting.RelatedObjectDoesNotExist:
            return None
        return TargetingSerializer(targeting).data


class CampaignCreateSerializer(serializers.Serializer):
    """POST campaigns/: step 1 of the builder, which starts the draft."""

    company = serializers.IntegerField(min_value=1)
    objective = serializers.ChoiceField(choices=Campaign.Objective.choices)
    name = serializers.CharField(max_length=120, required=False, allow_blank=True)


# -- money ---------------------------------------------------------------------


class TransactionSerializer(serializers.ModelSerializer):
    """One line of a company's ledger, as its Billing page lists it."""

    kind_label = serializers.CharField(source='get_kind_display', read_only=True)
    campaign_name = serializers.SerializerMethodField()

    class Meta:
        model = AdTransaction
        fields = [
            'id',
            'kind',
            'kind_label',
            'campaign',
            'campaign_name',
            'amount_cents',
            'balance_after_cents',
            'reserved_change_cents',
            'reserved_after_cents',
            'description',
            'created_at',
        ]
        read_only_fields = fields

    def get_campaign_name(self, obj):
        if obj.campaign_id is None:
            return None
        return obj.campaign.name or 'Untitled campaign'


class CheckoutSerializer(serializers.Serializer):
    """POST companies/{id}/checkout/: how much to add, and where Stripe returns.

    The return addresses are checked against the origins this API already
    trusts, as billing's are: Stripe redirects a just-paid customer there.
    """

    amount_cents = serializers.IntegerField(min_value=1, max_value=MAX_TOP_UP_CENTS)
    success_url = serializers.URLField(validators=[_validate_redirect_url])
    cancel_url = serializers.URLField(validators=[_validate_redirect_url])


class BudgetIncreaseSerializer(serializers.Serializer):
    """POST campaigns/{id}/budget/: the new, larger total."""

    total_budget_cents = serializers.IntegerField(min_value=1, max_value=MAX_BUDGET_CENTS)
