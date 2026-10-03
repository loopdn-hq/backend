"""Routes for the admin site, mounted at /api/v1/admin/ by loopd.urls.

One router registration per area; each viewset carries its own gates
(platform_admin/permissions.py).
"""
from rest_framework.routers import SimpleRouter

from platform_admin.viewsets.v1.campaigns import CampaignReviewViewSet

app_name = 'platform_admin'

router = SimpleRouter()
router.register('campaigns', CampaignReviewViewSet, basename='campaign')

urlpatterns = [*router.urls]
