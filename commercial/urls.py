"""Routes for the commercial app, mounted at /api/v1/commercial/ by loopd.urls.

No path here may contain `ads`, `ad-` or `advert`: ad blockers drop requests
that match, and the people running this dashboard are the likeliest to have
one installed. That is why a company is `companies/`, never `advertisers/`.
"""
from django.urls import path
from rest_framework.routers import SimpleRouter

from commercial.viewsets.v1.active_company import ActiveCompanyView
from commercial.viewsets.v1.companies import (
    CompanyViewSet,
    IndustryListView,
    MarketListView,
)

app_name = 'commercial'

router = SimpleRouter()
router.register('companies', CompanyViewSet, basename='company')

urlpatterns = [
    path('active-company/', ActiveCompanyView.as_view(), name='active-company'),
    path('industries/', IndustryListView.as_view(), name='industries'),
    path('markets/', MarketListView.as_view(), name='markets'),
    *router.urls,
]
