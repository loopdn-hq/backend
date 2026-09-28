"""Version 1 of the permissions matrix.

    GET  /api/v1/rbac/matrix/         what each role may use, grouped
    POST /api/v1/rbac/matrix/set/     change one cell

Reading and writing are separate features - `platform.permissions.view` and
`platform.permissions.edit` - so a support role can be given the first without
the second.

The write path's guards live in `rbac/services.py:set_grant`, not here: a
management command and a migration must obey them too, and a guard that only
HTTP respects is not a guard.

The GET returns the same structure the screen renders, so it is also the thing
to paste into a support conversation when somebody asks what a commercial user
can do.
"""
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rbac import services
from rbac.models import Feature, UserRole
from rbac.resolver import require
from rbac.serializers import SetGrantSerializer

#: The order roles read in, per axis. Most-privileged first, because the
#: question the screen answers is "what does this role NOT have" and the
#: baseline belongs at the right-hand edge to compare against.
#:
#: A role appearing in the data but not here is appended alphabetically rather
#: than dropped - a matrix that silently omits a column is worse than an ugly
#: one, and a company's roles land in exactly that position.
ROLE_ORDER = {
    Feature.Axis.ACCOUNT: [
        UserRole.Role.ADMIN, UserRole.Role.COMMERCIAL, UserRole.Role.STANDARD,
    ],
    Feature.Axis.CONVERSATION: ['admin', 'member'],
    Feature.Axis.COMPANY: ['owner', 'manager', 'analyst'],
}

AXIS_LABELS = {
    Feature.Axis.ACCOUNT: 'Account roles',
    Feature.Axis.CONVERSATION: 'Group roles',
    Feature.Axis.COMPANY: 'Company roles',
}


def _ordered_roles(axis, seen):
    """Known roles in their reading order, then anything else alphabetically.

    Plain strings, not TextChoices members: these become JSON keys and dict
    lookups downstream, and a `str` subclass that reprs as
    `UserRole.Role.ADMIN` is a debugging trap for no benefit.
    """
    preferred = [str(role) for role in ROLE_ORDER.get(axis, []) if role in seen]
    return preferred + sorted(str(role) for role in seen - set(preferred))


def build_matrix():
    """The whole matrix, in three queries and no per-row lookups.

    Shared with the frontend page and with anybody debugging in a shell, so the
    grouping is described once rather than twice.
    """
    features = list(
        Feature.objects.prefetch_related('role_features').order_by(
            'axis', 'area', 'sort_order', 'key'
        )
    )

    by_axis = {}
    for feature in features:
        by_axis.setdefault(feature.axis, []).append(feature)

    axes = []
    for axis, axis_features in by_axis.items():
        seen = {
            cell.role for feature in axis_features
            for cell in feature.role_features.all()
        }
        roles = _ordered_roles(axis, seen)

        areas = []
        for feature in axis_features:
            if not areas or areas[-1]['area'] != feature.area:
                areas.append({'area': feature.area, 'features': []})

            grants = {
                cell.role: cell.allowed for cell in feature.role_features.all()
            }
            areas[-1]['features'].append({
                'key': feature.key,
                'label': feature.label,
                'description': feature.description,
                # Never grantable below account admin, whatever the matrix
                # says. The screen renders these as an em dash rather than an
                # unticked box: "not allowed" and "cannot be allowed" are
                # different facts and a reader should not have to guess.
                'locked': feature.is_lockable,
                # A cell the seeder never wrote. Denies, exactly like False,
                # but reads as an oversight rather than a decision - which is
                # why rbac/checks.py warns about it.
                'grants': {role: grants.get(role) for role in roles},
            })

        axes.append({
            'axis': axis,
            'label': AXIS_LABELS.get(axis, axis.title()),
            'roles': roles,
            'areas': areas,
        })

    order = list(ROLE_ORDER)
    axes.sort(key=lambda a: order.index(a['axis']) if a['axis'] in order else 99)
    return {'axes': axes}


class MatrixViewSet(viewsets.ViewSet):
    """Account admins only, gated on its own feature.

    Deliberately NOT `platform.role.grant`: reading who may do what is a
    narrower thing than changing it, and step 5's grant API already owns the
    latter. Keeping them separate is what lets a future support role read this
    without being able to hand out roles.
    """

    permission_classes = [IsAuthenticated]

    success_messages = {
        'list': 'Permissions matrix fetched.',
        'set': 'Permission updated.',
    }

    def list(self, request):
        require(request.user, 'platform.permissions.view')
        return Response(build_matrix())

    @action(detail=False, methods=['post'])
    def set(self, request):
        """Change one cell.

        One cell per call rather than a whole matrix: a bulk write would need
        to decide what happens when the third of twenty changes is refused, and
        "some of your edits applied" is the worst answer a permissions screen
        can give.
        """
        require(request.user, 'platform.permissions.edit')

        serializer = SetGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            services.set_grant(
                data['feature'], data['role'], data['allowed'],
                by=request.user, note=data.get('note', ''),
            )
        except services.RoleChangeRefused as exc:
            # 400, not 403: the caller IS allowed to edit the matrix, and this
            # particular edit is refused. A 403 would read as "you cannot edit
            # permissions", which is the opposite of what happened.
            raise ValidationError(str(exc))

        return Response(build_matrix())
