"""Version 1 of the invite API - one endpoint.

    POST /api/v1/users/invite/   email somebody an invitation to Loopd'n

For the New conversation box, when the address typed there belongs to nobody a
conversation can reach. What the answer may and may not reveal is decided in
`user/invites.py`; this only carries it.
"""
from rest_framework import status, viewsets
from rest_framework.exceptions import Throttled
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from user import invites
from user.serializers import AppInviteSerializer


class InviteViewSet(viewsets.GenericViewSet):
    """Invitations to the product itself, rather than to one group."""

    permission_classes = [IsAuthenticated]
    serializer_class = AppInviteSerializer
    http_method_names = ['post', 'head', 'options']

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            outcome = invites.invite_to_app(
                request.user, serializer.validated_data['email']
            )
        except invites.TooManyInvites:
            raise Throttled(
                detail='That is a lot of invitations at once. Try again later.'
            )

        # Set here rather than in a `success_messages` dict: one action, two
        # true things to say, and the renderer reads this attribute off the view.
        self.success_message = (
            'They already have an account.'
            if outcome == invites.ALREADY_HERE
            else 'Invitation sent.'
        )
        return Response({'outcome': outcome}, status=status.HTTP_200_OK)
