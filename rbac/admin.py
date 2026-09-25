"""Read and correct the three tables by hand.

Registered this early - before anything reads these rows - because step 2 seeds
data that needs REVIEWING, and this is the tool it gets reviewed in. It is also
the tool for correcting a grant that turns out wrong, which is the whole reason
step 2 lands before the resolver in step 3.
"""
from django.contrib import admin

from rbac.models import Feature, RoleAuditLog, RoleFeature, UserRole


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'granted_by', 'granted_at')
    list_filter = ('role',)
    search_fields = ('user__username', 'user__email')
    autocomplete_fields = ('user', 'granted_by')
    ordering = ('user_id', 'role')


class RoleFeatureInline(admin.TabularInline):
    """The matrix, one feature at a time.

    Editing a feature shows its row across every role, which is the reading
    the permissions screen will eventually give in both directions.
    """

    model = RoleFeature
    extra = 0
    fields = ('role', 'allowed')


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = (
        'key', 'label', 'area', 'axis', 'default_allowed', 'is_lockable',
        'sort_order',
    )
    list_filter = ('area', 'axis', 'default_allowed', 'is_lockable')
    search_fields = ('key', 'label', 'description')
    ordering = ('area', 'sort_order', 'key')
    inlines = [RoleFeatureInline]


@admin.register(RoleFeature)
class RoleFeatureAdmin(admin.ModelAdmin):
    list_display = ('role', 'feature', 'allowed')
    list_filter = ('role', 'allowed', 'feature__area', 'feature__axis')
    search_fields = ('feature__key', 'feature__label')
    list_select_related = ('feature',)
    ordering = ('feature__area', 'feature__sort_order', 'role')


@admin.register(RoleAuditLog)
class RoleAuditLogAdmin(admin.ModelAdmin):
    """Read-only, because an audit log somebody can edit is not one.

    Append-only in the database too: nothing deletes these rows, and a user
    being deleted nulls the FK while leaving the label behind.
    """

    list_display = (
        'created_at', 'action', 'actor_label', 'subject_label', 'role',
        'feature_key',
    )
    list_filter = ('action', 'role')
    search_fields = ('actor_label', 'subject_label', 'role', 'feature_key', 'note')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
