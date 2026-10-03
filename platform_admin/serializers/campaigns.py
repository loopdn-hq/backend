"""Campaign review: the queue, one campaign as a reviewer reads it, and the
two decisions.

The campaign's own fields, audience and creatives are commercial's shapes,
reused rather than restated. What a reviewer sees beyond the company's view:
the company behind it, when it was submitted, the automatic checks, and the
history with each reviewer named (companies see "Loopd'n").
"""
from rest_framework import serializers

from commercial.markets import REGIONS, Country
from commercial.models import CampaignEvent, Company
from commercial.serializers import CampaignSerializer, CreativeSerializer
from commercial.services import review


class CompanySummarySerializer(serializers.ModelSerializer):
    """Who is behind a campaign: enough to judge it, nothing about money."""

    industry_label = serializers.CharField(source='industry.label', read_only=True)
    country_name = serializers.CharField(source='get_country_display', read_only=True)
    owner = serializers.CharField(source='owner.username', read_only=True)

    class Meta:
        model = Company
        fields = [
            'id',
            'name',
            'status',
            'industry_label',
            'country',
            'country_name',
            'website',
            'contact_email',
            'owner',
        ]
        read_only_fields = fields


class LabelledCampaignSerializer(CampaignSerializer):
    """A campaign with its objective and status in words, as the admin site
    shows them - so it keeps no copy of the labels the models already have.
    """

    objective_label = serializers.CharField(source='get_objective_display', read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)

    class Meta(CampaignSerializer.Meta):
        fields = [*CampaignSerializer.Meta.fields, 'objective_label', 'status_label']
        read_only_fields = fields


class ReviewQueueSerializer(LabelledCampaignSerializer):
    """One row of the queue."""

    company_name = serializers.CharField(source='company.name', read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True)
    creative_count = serializers.IntegerField(read_only=True)

    class Meta(LabelledCampaignSerializer.Meta):
        fields = [
            *LabelledCampaignSerializer.Meta.fields,
            'company_name',
            'submitted_at',
            'creative_count',
        ]
        read_only_fields = fields


class ReviewAudienceSerializer(serializers.Serializer):
    """A campaign's audience with names, not codes: a reviewer reads it as is.
    An empty list means "any", as everywhere.
    """

    categories = serializers.SerializerMethodField()
    regions = serializers.SerializerMethodField()
    min_group_size = serializers.IntegerField(read_only=True)
    max_group_size = serializers.IntegerField(read_only=True)

    def get_categories(self, obj):
        return [
            {'slug': category.slug, 'label': category.label}
            for category in obj.categories.all()
        ]

    def get_regions(self, obj):
        names = dict(REGIONS.get(Country(obj.campaign.company.country), []))
        return [{'code': code, 'name': names.get(code, code)} for code in obj.regions]


class FindingSerializer(serializers.Serializer):
    code = serializers.CharField()
    severity = serializers.CharField()
    message = serializers.CharField()
    creative_id = serializers.IntegerField(allow_null=True)


class ReviewEventSerializer(serializers.ModelSerializer):
    """A line of history as an admin reads it: who, by name, and the details."""

    kind_label = serializers.CharField(source='get_kind_display', read_only=True)
    by = serializers.SerializerMethodField()

    class Meta:
        model = CampaignEvent
        fields = ['kind', 'kind_label', 'by', 'note', 'metadata', 'created_at']
        read_only_fields = fields

    def get_by(self, obj):
        """The person's name, or null for the system."""
        return obj.actor_label or None


class ReviewCampaignSerializer(LabelledCampaignSerializer):
    """One campaign with everything a reviewer decides on."""

    company = CompanySummarySerializer(read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True)
    targeting = serializers.SerializerMethodField()
    creatives = CreativeSerializer(many=True, read_only=True)
    events = ReviewEventSerializer(many=True, read_only=True)
    findings = serializers.SerializerMethodField()

    class Meta(LabelledCampaignSerializer.Meta):
        fields = [
            *LabelledCampaignSerializer.Meta.fields,
            'submitted_at',
            'targeting',
            'creatives',
            'events',
            'findings',
        ]
        read_only_fields = fields

    def get_targeting(self, obj):
        targeting = getattr(obj, 'targeting', None)
        return None if targeting is None else ReviewAudienceSerializer(targeting).data

    def get_findings(self, obj):
        """What the automatic checks find now, blockers first."""
        return FindingSerializer(review.findings(obj), many=True).data


class ApproveSerializer(serializers.Serializer):
    """POST campaigns/{id}/approve/: an optional note, for the history."""

    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class CreativeNoteSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1)
    note = serializers.CharField(max_length=2000)


class RejectSerializer(serializers.Serializer):
    """POST campaigns/{id}/reject/: why, and optionally which creatives.

    `note` is what the company reads; required. Each entry in `creatives`
    marks one creative rejected, with its own note.
    """

    note = serializers.CharField(max_length=2000)
    creatives = CreativeNoteSerializer(many=True, required=False)


class MatchCheckSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()
    passed = serializers.BooleanField()
    detail = serializers.CharField()


class GroupMatchSerializer(serializers.Serializer):
    """GET campaigns/{id}/match/?group=: one group against the audience, each
    rule named (commercial/services/eligibility.py)."""

    group = serializers.SerializerMethodField()
    matches = serializers.BooleanField()
    checks = MatchCheckSerializer(many=True)

    def get_group(self, obj):
        group = self.context['group']
        return {
            'id': group.pk,
            'name': group.name,
            'member_count': obj['member_count'],
        }
