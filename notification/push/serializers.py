"""What a browser sends when it agrees to be notified."""
from rest_framework import serializers

from notification.models import PushSubscription


class PushSubscriptionSerializer(serializers.Serializer):
    """A browser's PushSubscription, in the shape the browser itself produces.

    `subscription.toJSON()` in the web app gives exactly `{endpoint, keys:
    {p256dh, auth}}`, so that is what this accepts - the client passes the
    object straight through instead of taking it apart, and there is one less
    place for the two halves to disagree about a field name.
    """

    endpoint = serializers.CharField(max_length=500)
    keys = serializers.DictField(child=serializers.CharField(), write_only=True)
    # What the browser calls itself. Optional and display-only; the Settings
    # screen shows it so somebody can tell their laptop from their phone.
    user_agent = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )

    def validate_keys(self, value):
        missing = [name for name in ('p256dh', 'auth') if not value.get(name)]
        if missing:
            raise serializers.ValidationError(
                f'Missing {", ".join(missing)} - this is not a push subscription.'
            )
        return value

    def save(self, user):
        """Store it, or move it to this account if the browser changed hands.

        `update_or_create` on the ENDPOINT, which is the browser's identity.
        Two people using one laptop is the case that matters: the second sign-in
        produces the same endpoint, and without this the row would still point
        at the first account - so one person's messages would appear on a
        screen the other person is looking at.
        """
        data = self.validated_data
        keys = data['keys']

        subscription, _ = PushSubscription.objects.update_or_create(
            endpoint=data['endpoint'],
            defaults={
                'user': user,
                'p256dh': keys['p256dh'],
                'auth': keys['auth'],
                'user_agent': data.get('user_agent', '')[:200],
            },
        )
        return subscription


class PushSubscriptionRemoveSerializer(serializers.Serializer):
    """The endpoint to forget, when a browser turns notifications off."""

    endpoint = serializers.CharField(max_length=500)
