"""The lead form, for members in the chat app (slice 7a).

    POST lead-form/  {token}                              open it: counts the
                                                          press, returns where
                                                          to start
    POST leads/      {token, name, phone, email, message} send it

`chat.access`, like serve/ and impressions/: these are a member's, not a
company's. The token is the card's click token, and only the member it was
served to may use it. Thin: the rules are services/leads.py's. A company
reads its leads at companies/{id}/leads/.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from commercial.serializers import LeadFormSerializer, LeadSendSerializer
from commercial.services import leads
from commercial.viewsets.v1.refusals import service_errors
from commercial.views import client_ip
from rbac.permissions import feature_required


class LeadFormView(APIView):
    permission_classes = [IsAuthenticated, feature_required('chat.access')]

    def post(self, request):
        body = LeadFormSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        with service_errors():
            form = leads.open_form(
                request.user,
                body.validated_data['token'],
                ip=client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )
        response = Response(form)
        response.message = 'Form opened.'
        return response


class LeadSendView(APIView):
    permission_classes = [IsAuthenticated, feature_required('chat.access')]

    def post(self, request):
        body = LeadSendSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        details = dict(body.validated_data)
        token = details.pop('token')
        with service_errors():
            leads.send(request.user, token, **details)
        response = Response({'sent': True}, status=status.HTTP_201_CREATED)
        response.message = 'Details sent.'
        return response
