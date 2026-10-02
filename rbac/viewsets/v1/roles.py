"""Version 1 of the account-role API.

    GET    /api/v1/rbac/roles/          every account role, newest first
    POST   /api/v1/rbac/roles/grant/    give somebody a role
    POST   /api/v1/rbac/roles/revoke/   take one away
    GET    /api/v1/rbac/roles/audit/    who changed what, and when

Account admins only, all of it. Unlike conversations - where the queryset is
narrowed to the caller's own rows - there is no narrower view of this to give
anybody else: "which roles does this person hold" is the whole resource, and a
non-admin has no business reading it for anybody, including themselves. Their
own access arrives as features from `/users/me/features/` in step 6, which is
what a client actually needs.

`platform.role.grant` is `is_lockable`, so no matrix edit can ever open this to
`standard` or `commercial` - see rbac/checks.py:lockable_features_are_admin_only.
"""
from django.contrib.auth.models import User
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rbac import services
from rbac.models import RoleAuditLog, UserRole
from rbac.resolver import require
from rbac.serializers import (
    GrantRoleSerializer,
    RevokeRoleSerializer,
    RoleAuditLogSerializer,
    UserRoleSerializer,
)


class RolePagination(PageNumberPagination):
    page_size_query_param = 'page_size'
    max_page_size = 200


class RoleViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = UserRoleSerializer
    pagination_class = RolePagination

    serializer_classes = {
        'grant': GrantRoleSerializer,
        'revoke': RevokeRoleSerializer,
        'audit': RoleAuditLogSerializer,
    }

    success_messages = {
        'list': 'Account roles fetched.',
        'grant': 'Role granted.',
        'revoke': 'Role revoked.',
        'audit': 'Audit log fetched.',
    }

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.serializer_class)

    def initial(self, request, *args, **kwargs):
        """One gate for the whole viewset, checked before any handler runs.

        A permission check rather than a narrowed queryset, which is the
        exception `CLAUDE.md` allows for: there is no subset of this resource
        that belongs to a non-admin, so there is nothing to narrow TO. The rule
        exists to stop a broad queryset being guarded by a check somebody
        forgets; here the queryset is deliberately global and the check is the
        boundary, in one place rather than on four handlers.
        """
        super().initial(request, *args, **kwargs)
        # The admin site's door, then this resource's own feature.
        require(request.user, 'platform.access')
        require(request.user, 'platform.role.grant')

    def get_queryset(self):
        rows = UserRole.objects.select_related('user', 'granted_by')
        role = self.request.query_params.get('role')
        if role:
            rows = rows.filter(role=role)
        username = self.request.query_params.get('username')
        if username:
            rows = rows.filter(user__username__iexact=username)
        return rows.order_by('-granted_at', '-id')

    @action(detail=False, methods=['post'])
    def grant(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            row = services.grant(
                data['user'], data['role'], by=request.user,
                note=data.get('note', ''),
            )
        except services.RoleChangeRefused as exc:
            raise ValidationError(str(exc))

        return Response(UserRoleSerializer(row).data)

    @action(detail=False, methods=['post'])
    def revoke(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            row = services.revoke(
                data['user'], data['role'], by=request.user,
                note=data.get('note', ''),
            )
        except services.RoleChangeRefused as exc:
            # 400 rather than 403: the caller is allowed to revoke roles, and
            # this particular revocation is refused. A 403 would read as "you
            # are not an admin", which is the opposite of what happened.
            raise ValidationError(str(exc))

        if row is None:
            return Response({'user': data['user'].pk, 'role': data['role'],
                             'revoked': False})
        return Response({'user': data['user'].pk, 'role': data['role'],
                         'revoked': True})

    @action(detail=False, methods=['get'])
    def audit(self, request):
        rows = RoleAuditLog.objects.all()
        username = request.query_params.get('username')
        if username:
            rows = rows.filter(subject_label__iexact=username)

        page = self.paginate_queryset(rows)
        serializer = RoleAuditLogSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)
