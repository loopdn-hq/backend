"""Campaign review (slice 2 of docs/campaigns_final_plan.md).

    GET  campaigns/                 the queue: waiting for review, oldest first.
                                    `?status=` another status or `all`;
                                    `?search=` a campaign's or company's name
    GET  campaigns/{id}/            one campaign, with the automatic checks
    POST campaigns/{id}/approve/    {note?}
    POST campaigns/{id}/reject/     {note, creatives?: [{id, note}]}
    GET  campaigns/{id}/match/?group={id}
                                    does that group match the audience, and
                                    why not (slice 4)

Thin: the decisions are commercial/services/transitions.py's (approve,
reject), the checks commercial/services/review.py's, the group match
commercial/services/eligibility.py's. The queryset is every
company's campaigns - the one kind of route that is not scoped to the caller,
which is why it sits behind two features, not one.
"""
from django.db.models import Count, Max, Prefetch, Q
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from chat.models import Conversation
from commercial.models import Campaign, CampaignEvent, Creative
from commercial.services import eligibility, transitions
from commercial.viewsets.v1.refusals import service_errors
from platform_admin.pagination import AdminPagination
from platform_admin.permissions import CanReviewCampaigns, CanUseAdmin
from platform_admin.serializers.campaigns import (
    ApproveSerializer,
    GroupMatchSerializer,
    RejectSerializer,
    ReviewCampaignSerializer,
    ReviewQueueSerializer,
)


class CampaignReviewViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, CanUseAdmin, CanReviewCampaigns]
    pagination_class = AdminPagination

    success_messages = {
        'list': 'Campaigns fetched.',
        'retrieve': 'Campaign fetched.',
        'approve': 'Campaign approved.',
        'reject': 'Campaign sent back to the company.',
        'match': 'Group checked.',
    }

    def get_queryset(self):
        campaigns = Campaign.objects.select_related(
            'company__industry', 'company__owner'
        ).annotate(
            # When it was last submitted: the queue's order, oldest first.
            submitted_at=Max(
                'events__created_at', filter=Q(events__kind=CampaignEvent.Kind.SUBMITTED)
            ),
        )
        if self.action == 'list':
            return self._filtered(campaigns).annotate(
                creative_count=Count('creatives', distinct=True)
            )
        return campaigns.select_related('targeting').prefetch_related(
            'targeting__categories',
            Prefetch('creatives', queryset=Creative.objects.order_by('created_at', 'pk')),
            'events',
        )

    def get_serializer_class(self):
        return ReviewQueueSerializer if self.action == 'list' else ReviewCampaignSerializer

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        body = ApproveSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        with service_errors():
            transitions.approve(
                self.get_object(), request.user, note=body.validated_data.get('note', '')
            )
        return Response(self._detail(pk))

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        body = RejectSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        creative_notes = {
            row['id']: row['note'] for row in body.validated_data.get('creatives', [])
        }
        with service_errors():
            transitions.reject(
                self.get_object(),
                request.user,
                body.validated_data['note'],
                creative_notes=creative_notes,
            )
        return Response(self._detail(pk))

    @action(detail=True, methods=['get'])
    def match(self, request, pk=None):
        raw = request.query_params.get('group', '').strip()
        if not raw.isdigit():
            raise ValidationError({'group': "Say which group, by its number: ?group=42."})
        group = (
            Conversation.objects.filter(kind=Conversation.Kind.GROUP, pk=int(raw))
            .select_related('category', 'created_by')
            .first()
        )
        if group is None:
            raise NotFound('No group has that number.')
        result = eligibility.explain(self.get_object(), group)
        return Response(GroupMatchSerializer(result, context={'group': group}).data)

    def _filtered(self, campaigns):
        """The queue by default; `?status=` and `?search=` narrow it otherwise."""
        params = self.request.query_params
        wanted = params.get('status', '').strip() or Campaign.Status.PENDING_REVIEW
        if wanted != 'all':
            if wanted not in Campaign.Status.values:
                raise ValidationError({'status': f'"{wanted}" is not a campaign status.'})
            campaigns = campaigns.filter(status=wanted)

        search = params.get('search', '').strip()
        if search:
            campaigns = campaigns.filter(
                Q(name__icontains=search) | Q(company__name__icontains=search)
            )

        # Waiting longest first, so nothing sits at the bottom of the queue.
        # Anything else: newest first.
        if wanted == Campaign.Status.PENDING_REVIEW:
            return campaigns.order_by('submitted_at', 'pk')
        return campaigns.order_by('-created_at', '-pk')

    def _detail(self, pk):
        """The campaign as it is now, read again with everything prefetched."""
        fresh = self.get_queryset().get(pk=pk)
        return ReviewCampaignSerializer(fresh, context=self.get_serializer_context()).data
