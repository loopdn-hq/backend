"""DRF permission classes that check one RBAC feature.

The feature gate of CLAUDE.md's rule 3, as a class: a view lists what it needs
in `permission_classes` and DRF checks it before any handler runs, answering
403 with the feature's own sentence (`rbac.resolver.require`). It is never the
access control on an object - that is the queryset.
"""
from rest_framework.permissions import BasePermission

from rbac.resolver import require


def feature_required(feature_key):
    """A permission class requiring `feature_key` on the account axis.

        permission_classes = [IsAuthenticated, feature_required('platform.access')]

    Put it after IsAuthenticated, so a missing token is a 401, not a 403.
    """

    class FeatureRequired(BasePermission):
        def has_permission(self, request, view):
            require(request.user, feature_key)
            return True

    FeatureRequired.feature_key = feature_key
    FeatureRequired.__name__ = FeatureRequired.__qualname__ = (
        f'FeatureRequired[{feature_key}]'
    )
    return FeatureRequired
