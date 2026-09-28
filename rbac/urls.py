"""Routes for the rbac app, mounted at /api/v1/rbac/ by loopd.urls."""
from rest_framework.routers import SimpleRouter

from rbac.viewsets.v1.matrix import MatrixViewSet
from rbac.viewsets.v1.roles import RoleViewSet

app_name = 'rbac'

router = SimpleRouter()
router.register('roles', RoleViewSet, basename='role')
router.register('matrix', MatrixViewSet, basename='matrix')

urlpatterns = router.urls
