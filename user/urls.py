"""Routes for the user app, mounted at /api/v1/users/ by loopd.urls.

The prefix is empty because the version and the resource name both live in
the mount point, keeping /api/v2/users/ a one-line change later.
"""
from rest_framework.routers import SimpleRouter

from user.viewsets.v1.invites import InviteViewSet
from user.viewsets.v1.settings import SettingsViewSet
from user.viewsets.v1.users import UserViewSet

app_name = 'user'

router = SimpleRouter()
# me/settings/ and me/settings/<screen>/. They cannot collide with the user
# viewset's detail route, which matches a single path segment.
router.register('me/settings', SettingsViewSet, basename='settings')
# `invite` IS a single segment, so the user viewset's detail route would read
# it as a user id. Routes are matched in order and this one is registered first.
router.register('invite', InviteViewSet, basename='invite')
router.register('', UserViewSet, basename='user')

urlpatterns = router.urls
