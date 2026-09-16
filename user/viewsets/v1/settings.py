"""Version 1 of the settings API - one resource per settings screen.

    GET    /api/v1/users/me/settings/            every screen at once
    GET    /api/v1/users/me/settings/<screen>/   one screen
    PATCH  /api/v1/users/me/settings/<screen>/   change some of one screen's fields

<screen> is appearance, messaging, notifications, privacy or security - the
keys of `user.serializers.SETTINGS_SERIALIZERS`. Always the signed-in user's
own settings: there is no id in the URL, so there is no way to name anybody
else's.

PATCH only, no PUT: a screen saves one switch at a time, and a PUT that had to
resend every field would let a stale tab overwrite a change made in another.
A field a screen shows but may not write (the two-factor pair) is ignored if
sent, the same as `username` on /me/.
"""
from rest_framework import mixins, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from user.serializers import SETTINGS_SERIALIZERS


class SettingsViewSet(
    mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet
):
    """The signed-in user's settings, one screen at a time."""

    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'patch', 'head', 'options']

    # The screen name stands where an id would. Any lowercase word routes, so an
    # unknown one gets the API's own 404 envelope rather than Django's HTML page.
    lookup_field = 'screen'
    lookup_value_regex = '[a-z]+'

    success_messages = {
        'list': 'Settings fetched.',
        'retrieve': 'Settings fetched.',
        'partial_update': 'Settings updated.',
    }

    def list(self, request):
        """Every screen, keyed by name - what the settings page loads first."""
        return Response({
            screen: serializer_class(
                serializer_class.Meta.model.for_user(request.user),
                context=self.get_serializer_context(),
            ).data
            for screen, serializer_class in SETTINGS_SERIALIZERS.items()
        })

    def get_serializer_class(self):
        try:
            return SETTINGS_SERIALIZERS[self.kwargs[self.lookup_field]]
        except KeyError:
            raise NotFound('There is no settings screen by that name.') from None

    def get_object(self):
        # for_user, not a lookup: rows are made at sign-in, but a token issued
        # before that still gets its row made here rather than a 404.
        return self.get_serializer_class().Meta.model.for_user(self.request.user)
