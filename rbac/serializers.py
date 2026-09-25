"""Shapes for the role grant API."""
from django.contrib.auth.models import User
from rest_framework import serializers

from rbac.models import RoleAuditLog, UserRole


class UserRoleSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    granted_by_username = serializers.SerializerMethodField()

    class Meta:
        model = UserRole
        fields = ['id', 'user', 'username', 'role', 'granted_by_username', 'granted_at']
        read_only_fields = fields

    def get_granted_by_username(self, obj):
        # Blank rather than null for a system grant - a migration or the signup
        # signal - so a client renders one thing instead of branching.
        return obj.granted_by.username if obj.granted_by_id else ''


class GrantRoleSerializer(serializers.Serializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    role = serializers.ChoiceField(choices=UserRole.Role.choices)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)


class RevokeRoleSerializer(GrantRoleSerializer):
    pass


class RoleAuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = RoleAuditLog
        fields = [
            'id', 'action', 'actor_label', 'subject_label', 'role',
            'feature_key', 'note', 'created_at',
        ]
        read_only_fields = fields


class SetGrantSerializer(serializers.Serializer):
    """One cell of the permissions matrix.

    `role` is a free string rather than a ChoiceField: roles come from three
    vocabularies depending on the feature's axis - account, conversation and
    later the advertiser org - so the model cannot name one list. The service
    validates it against the feature it is being set on.
    """

    feature = serializers.CharField(max_length=64)
    role = serializers.CharField(max_length=16)
    allowed = serializers.BooleanField()
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)
