"""Routes for the otp app, mounted at /api/v1/users/me/phone/ by loopd.urls.

The prefix is empty because the version and the resource path both live in the
mount point, matching how the user and chat apps are wired.
"""
from rest_framework.routers import SimpleRouter

from otp.viewsets.v1.otp import PhoneVerificationViewSet

app_name = 'otp'

router = SimpleRouter()
router.register('', PhoneVerificationViewSet, basename='phone')

urlpatterns = router.urls
