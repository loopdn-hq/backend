"""The chat app's side of campaigns: a card to show, and "it was seen" (slice 5).

    GET  serve/?placement=chat&conversation={id}   a card for this member in
                                                   this group, or null
    POST impressions/  {token}                     the card was seen

For members, not companies: `chat.access`, and the group must be one the
caller is an active member of - anybody else's is a 404, as everywhere. Thin:
which card is services/serving.py's, recording a view services/measurement.py's.

A served card is never stored: the answer carries a signed token, and only a
view writes a row. `no-store`, so no cache between here and the app keeps one
person's card for another.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from chat.models import Conversation, Participant
from commercial.models import Campaign
from commercial.serializers import ImpressionSerializer, ServedCardSerializer
from commercial.services import measurement, serving
from commercial.viewsets.v1.refusals import service_errors
from rbac.permissions import feature_required


class ServeView(APIView):
    permission_classes = [IsAuthenticated, feature_required('chat.access')]

    def get(self, request):
        params = request.query_params
        placement = params.get('placement', Campaign.Placement.CHAT)
        raw = params.get('conversation', '').strip()
        if not raw.isdigit():
            raise ValidationError({'conversation': 'Say which group, e.g. ?conversation=45.'})
        conversation = get_object_or_404(
            Conversation,
            pk=int(raw),
            participants__user=request.user,
            participants__status=Participant.Status.ACTIVE,
        )
        served = serving.get_ad(request.user, conversation, placement)
        response = Response(
            None
            if served is None
            else ServedCardSerializer(served, context={'request': request}).data
        )
        response.message = 'Nothing to show.' if served is None else 'Card fetched.'
        response['Cache-Control'] = 'no-store'
        return response


class ImpressionView(APIView):
    permission_classes = [IsAuthenticated, feature_required('chat.access')]

    def post(self, request):
        body = ImpressionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        with service_errors():
            _impression, created = measurement.record_view(
                request.user, body.validated_data['token']
            )
        response = Response(
            {'counted': True},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
        response.message = 'View counted.' if created else 'Already counted.'
        return response
