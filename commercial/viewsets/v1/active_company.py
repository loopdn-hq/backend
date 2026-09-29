"""Which company the dashboard opens on. Context, never authorization.

Nothing else reads the stored choice: every company-scoped route names its
company and checks it on its own. See services.resolve_active_company.
"""
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from commercial.models import Company
from commercial.permissions import CanUseCommercial
from commercial.serializers import ActiveCompanySerializer, CompanySerializer
from commercial.services import companies as services

COMPANY_NOT_FOUND_DETAIL = 'No company was found with that id.'


class ActiveCompanyView(APIView):
    permission_classes = [IsAuthenticated, CanUseCommercial]

    def get(self, request):
        """The company to open on, or null: then the dashboard goes to /setup/."""
        company = services.resolve_active_company(request.user)
        data = None
        if company is not None:
            data = CompanySerializer(company, context={'request': request}).data
        response = Response(data)
        response.message = 'Active company fetched.'
        return response

    def put(self, request):
        serializer = ActiveCompanySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            company = services.set_active_company(
                request.user, serializer.validated_data['company']
            )
        except Company.DoesNotExist:
            # The same answer for "not yours" as for "no such company".
            raise NotFound(COMPANY_NOT_FOUND_DETAIL)
        response = Response(CompanySerializer(company, context={'request': request}).data)
        response.message = 'Switched company.'
        return response
