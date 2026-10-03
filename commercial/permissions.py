from rest_framework.permissions import BasePermission

from rbac.resolver import require


class CanUseCommercial(BasePermission):
    """Every commercial route needs `commercial.access`: the commercial role
    has it, a standard account does not.

    This is the feature gate, and a 403 with the feature's own sentence. It
    is not the access control on any one company: that is the queryset,
    `Company.objects.manageable_by`, which turns somebody else's company
    into a 404. Put it after IsAuthenticated, so a missing token is a 401.
    """

    def has_permission(self, request, view):
        require(request.user, 'commercial.access')
        return True
