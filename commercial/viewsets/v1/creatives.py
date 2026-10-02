"""One creative of a campaign: edit it or remove it while the campaign is a draft.

Adding and listing are under the campaign (campaigns/{id}/creatives/). Thin:
services/campaigns.py decides, and refuses any change once the campaign has
been submitted. Somebody else's creative is a 404, because the queryset only
reaches creatives of campaigns the caller can manage.
"""
from rest_framework import status, viewsets
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from commercial.models import Campaign, Creative
from commercial.permissions import CanUseCommercial
from commercial.serializers import CreativeSerializer
from commercial.services import campaigns as services
from commercial.viewsets.v1.refusals import service_errors


class CreativeViewSet(viewsets.GenericViewSet):
    """
    PATCH  creatives/{id}/    edit (multipart to replace the image; image=null removes it)
    DELETE creatives/{id}/    remove
    """

    permission_classes = [IsAuthenticated, CanUseCommercial]
    serializer_class = CreativeSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    success_messages = {
        'partial_update': 'Creative saved.',
        'destroy': 'Creative removed.',
    }

    def get_queryset(self):
        return Creative.objects.filter(
            campaign__in=Campaign.objects.manageable_by(self.request.user)
        ).select_related('campaign__company')

    def partial_update(self, request, pk=None):
        creative = self.get_object()
        body = self.get_serializer(creative, data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        with service_errors():
            creative = services.update_creative(creative, **body.validated_data)
        return Response(self.get_serializer(creative).data)

    def destroy(self, request, pk=None):
        with service_errors():
            services.delete_creative(self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)
