"""Granting and revoking account roles.

Not in the viewset, for the same reason `chat/membership.py` is not: a role
change has guards that a management command, a migration and a future admin
action must all obey, and a guard that lives in a view is a guard only HTTP
respects.
"""
from django.db import IntegrityError, transaction

from rbac.models import Feature, RoleAuditLog, RoleFeature, UserRole


class RoleChangeRefused(Exception):
    """A grant or revocation that must not happen."""


def roles_for_axis(axis):
    """The role names that mean something on this axis.

    Three vocabularies, which is why `RoleFeature.role` is a CharField and not
    a FK - see rbac/models.py. This is the one place that knows which is which.
    """
    from chat.models import Participant

    if axis == Feature.Axis.ACCOUNT:
        return set(UserRole.Role.values)
    if axis == Feature.Axis.CONVERSATION:
        return set(Participant.Role.values)
    # The advertiser org's roles, defined in the matrix before the app exists
    # so the scope does not need revisiting when it arrives. See step 9.
    return {'owner', 'manager', 'analyst'}


def _admin_count(exclude_user=None):
    rows = UserRole.objects.filter(role=UserRole.Role.ADMIN)
    if exclude_user is not None:
        rows = rows.exclude(user=exclude_user)
    return rows.count()


@transaction.atomic
def grant(user, role, *, by=None, note=''):
    """Give `user` this account role. Idempotent.

    Re-granting a role somebody already holds returns the existing row and
    writes no audit entry: nothing changed, and a log full of no-ops is a log
    nobody reads.
    """
    if role not in UserRole.Role.values:
        raise RoleChangeRefused('That is not an account role.')

    try:
        with transaction.atomic():
            row = UserRole.objects.create(user=user, role=role, granted_by=by)
    except IntegrityError:
        # A SAVEPOINT, so a race on the unique constraint is recoverable
        # rather than poisoning the surrounding transaction. Same shape as
        # chat/services.py:create_message.
        existing = UserRole.objects.filter(user=user, role=role).first()
        if existing is None:
            raise
        return existing

    RoleAuditLog.record(
        RoleAuditLog.Action.GRANT, actor=by, subject=user, role=role, note=note,
    )
    return row


@transaction.atomic
def revoke(user, role, *, by=None, note=''):
    """Take an account role away. Idempotent.

    The last `admin` cannot be revoked, mirroring the last-admin guard in
    `chat/membership.py:set_role` and for the same reason: granting a role is
    itself an admin action, so a product with no account admin has no route
    back to having one. That is a database restore, not a support ticket.
    """
    row = UserRole.objects.select_for_update().filter(user=user, role=role).first()
    if row is None:
        return None

    if role == UserRole.Role.ADMIN and _admin_count(exclude_user=user) == 0:
        raise RoleChangeRefused(
            'This is the last account admin. Grant the role to somebody else '
            'first.'
        )

    row.delete()
    RoleAuditLog.record(
        RoleAuditLog.Action.REVOKE, actor=by, subject=user, role=role, note=note,
    )
    return row


@transaction.atomic
def start_as_commercial(user, *, note='Signed up on the commercial site.'):
    """Make a brand-new account a commercial one rather than a standard one.

    For signup only. Every account is created holding `standard`, by the signal
    in user/signals.py. One made on the commercial site (commercial.loopdn.com)
    holds `commercial` instead: the ads area and nothing else, so it has no
    chat (docs/rbac_plan.md, open question 1).

    The `standard` row is deleted rather than revoked. It has existed only for
    the length of this transaction, its creation was never audited either, and
    a REVOKE entry would record a change nobody made. The `commercial` grant is
    audited like any other.

    Never call this for an account that has been used: it takes `standard`, and
    with it chat, away.
    """
    UserRole.objects.filter(user=user, role=UserRole.Role.STANDARD).delete()
    return grant(user, UserRole.Role.COMMERCIAL, note=note)


@transaction.atomic
def set_grant(feature_key, role, allowed, *, by=None, note=''):
    """Change one cell of the permissions matrix.

    Two guards, and together they are what makes an editable matrix safe rather
    than an escalation path with a nice table on it.

    LOCKED FEATURES MAY NOT LEAVE `admin`.
    `is_lockable` means "only an account admin may ever hold this" - role
    granting, platform settings, editing this matrix. Allowing one below admin
    would let whoever can open this screen hand themselves role management,
    which is the exact failure the flag exists to prevent.

    AND A LOCKED FEATURE MAY NOT BE TAKEN FROM `admin` EITHER.
    The mirror of the last-admin guard in `chat/membership.py:set_role`, and
    the same reasoning: only admins hold these, so denying admin means NOBODY
    holds it, and the feature that grants roles cannot be granted back. That is
    a database restore, not a support ticket.
    """
    feature = Feature.objects.filter(key=feature_key).first()
    if feature is None:
        raise RoleChangeRefused(f'No such feature: {feature_key}.')

    valid = roles_for_axis(feature.axis)
    if role not in valid:
        # Roles come from three different vocabularies depending on the axis,
        # so `RoleFeature.role` is a plain CharField and nothing at the database
        # level would stop `commercial` being written onto a group feature. The
        # row would never match anything and would sit in the matrix reading
        # like a real denial.
        raise RoleChangeRefused(
            f'{role} is not a role on the {feature.axis} axis. '
            f'Expected one of: {", ".join(sorted(valid))}.'
        )

    if feature.is_lockable:
        if allowed and role != UserRole.Role.ADMIN:
            raise RoleChangeRefused(
                f'{feature.label} can only ever be held by an account admin.'
            )
        if not allowed and role == UserRole.Role.ADMIN:
            raise RoleChangeRefused(
                f'{feature.label} cannot be taken from account admins - '
                'nobody else may hold it, so nothing could grant it back.'
            )

    cell, created = RoleFeature.objects.select_for_update().get_or_create(
        feature=feature, role=role, defaults={'allowed': allowed},
    )
    if not created:
        if cell.allowed == allowed:
            # Nothing changed. No write, and no audit row - a log full of
            # no-ops is a log nobody reads.
            return cell
        cell.allowed = allowed
        cell.save(update_fields=['allowed'])

    RoleAuditLog.record(
        RoleAuditLog.Action.FEATURE_ALLOW if allowed
        else RoleAuditLog.Action.FEATURE_DENY,
        actor=by, role=role, feature=feature, note=note,
    )
    return cell
