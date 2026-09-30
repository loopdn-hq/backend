"""Version 1 of the WhatsApp API, mounted at /api/v1/whatsapp/.

CONNECTING IS OPTIONAL. Nothing here is reached from signup or login; the web
app calls these from two places - profile settings, and a group's "Invite by
WhatsApp" - and nowhere else.

Profile settings
    GET    config/                           what the browser needs to start
                                             Embedded Signup
    GET    connections/                      my connected numbers (empty: show
                                             "Connect WhatsApp Business")
    POST   connections/                      finish Embedded Signup
    GET    connections/<id>/                 one of them
    PATCH  connections/<id>/                 set its template names
    POST   connections/<id>/refresh/         re-read name/quality from Meta
    DELETE connections/<id>/                 disconnect it

One group
    GET    groups/<conversation>/            WhatsApp in this group, and my
                                             own opt-in
    PUT    groups/<conversation>/            attach one of my numbers (admin)
    DELETE groups/<conversation>/            detach it (admin or number owner)
    PUT    groups/<conversation>/consent/    my own opt-in, from the app
    POST   groups/<conversation>/invite/     invite somebody over WhatsApp

Access control is the queryset, as everywhere else: connections are filtered to
their owner, conversations to those the caller is an active member of, so
anybody else's is a 404.
"""
from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from chat import membership
from chat.models import ChannelConsent, Conversation, Participant
from whatsapp import graph, onboarding, outbound
from whatsapp.errors import ConnectionRefused
from whatsapp.models import GroupChannel, WhatsAppConnection
from whatsapp.serializers import (
    AttachSerializer,
    ConnectionSerializer,
    ConnectSerializer,
    ConsentSerializer,
    GroupChannelSerializer,
    InviteSerializer,
)
from whatsapp.throttles import WhatsAppConnectThrottle, WhatsAppInviteThrottle

WHATSAPP = ChannelConsent.Channel.WHATSAPP


class ConfigView(APIView):
    permission_classes = [IsAuthenticated]
    success_message = 'WhatsApp configuration fetched.'

    def get(self, request):
        # The app id and configuration id are public by design - they are in
        # the page that calls FB.login. The secret is not here and never is.
        return Response({
            'enabled': graph.is_configured() and bool(settings.WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID),
            'app_id': settings.WHATSAPP_APP_ID,
            'config_id': settings.WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID,
            'graph_version': settings.WHATSAPP_GRAPH_VERSION,
        })


class ConnectionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = ConnectionSerializer
    http_method_names = ['get', 'post', 'patch', 'delete']

    success_messages = {
        'list': 'WhatsApp connections fetched.',
        'retrieve': 'WhatsApp connection fetched.',
        'create': 'WhatsApp Business connected.',
        'partial_update': 'WhatsApp connection updated.',
        'refresh': 'WhatsApp connection refreshed.',
        'destroy': 'WhatsApp Business disconnected.',
    }

    def get_throttles(self):
        if self.action == 'create':
            return [WhatsAppConnectThrottle()]
        return []

    def get_queryset(self):
        return WhatsAppConnection.objects.filter(owner=self.request.user).exclude(
            status=WhatsAppConnection.Status.DISCONNECTED
        )

    def create(self, request, *args, **kwargs):
        serializer = ConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = onboarding.connect(request.user, **serializer.validated_data)
        return Response(ConnectionSerializer(connection).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def refresh(self, request, pk=None):
        connection = onboarding.refresh(self.get_object())
        return Response(ConnectionSerializer(connection).data)

    def destroy(self, request, *args, **kwargs):
        onboarding.disconnect(self.get_object(), reason='disconnected by owner')
        return Response(status=status.HTTP_200_OK)


def _my_conversation(request, conversation_id):
    return get_object_or_404(
        Conversation.objects.filter(
            kind=Conversation.Kind.GROUP,
            participants__user=request.user,
            participants__status=Participant.Status.ACTIVE,
        ).distinct(),
        pk=conversation_id,
    )


def _my_membership(conversation, user):
    return Participant.objects.select_related('user__profile').get(
        conversation=conversation, user=user, status=Participant.Status.ACTIVE
    )


def _group_payload(conversation, user):
    channel = GroupChannel.whatsapp_for(conversation)
    me = _my_membership(conversation, user)
    profile = getattr(user, 'profile', None)
    return {
        'connected': channel is not None,
        'channel': GroupChannelSerializer(channel).data if channel else None,
        'is_admin': me.is_admin,
        'my_consent': membership.consent_status(me, WHATSAPP),
        # Whether opting in could reach them at all - the app should offer
        # phone verification first when this is False.
        'my_number_verified': bool(profile and profile.phone_no_verified),
    }


class GroupWhatsAppView(APIView):
    permission_classes = [IsAuthenticated]
    success_messages = {
        'GET': 'WhatsApp settings fetched.',
        'PUT': 'WhatsApp connected to this group.',
        'DELETE': 'WhatsApp disconnected from this group.',
    }

    def get(self, request, conversation_id):
        conversation = _my_conversation(request, conversation_id)
        return Response(_group_payload(conversation, request.user))

    def put(self, request, conversation_id):
        conversation = _my_conversation(request, conversation_id)
        serializer = AttachSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        connection = get_object_or_404(
            WhatsAppConnection.objects.filter(owner=request.user), pk=data['connection']
        )
        channel = onboarding.attach(
            conversation, connection, request.user, keyword=data.get('keyword', '')
        )
        changed = []
        for name in ('delivery_enabled', 'replies_enabled'):
            if name in data:
                setattr(channel, name, data[name])
                changed.append(name)
        if changed:
            channel.save(update_fields=[*changed, 'updated_at'])
        return Response(_group_payload(conversation, request.user))

    def delete(self, request, conversation_id):
        conversation = _my_conversation(request, conversation_id)
        channel = GroupChannel.objects.filter(
            conversation=conversation, kind=GroupChannel.Kind.WHATSAPP
        ).select_related('whatsapp_connection').first()
        if channel is None:
            raise ConnectionRefused('This group is not on WhatsApp.', code='not_attached')
        onboarding.detach(channel, request.user)
        return Response(_group_payload(conversation, request.user))


class GroupConsentView(APIView):
    """A member's own opt-in to this group on WhatsApp, from inside the app.

    Theirs alone: nobody else can opt a member in. Opting in from the app is an
    explicit, recorded opt-in - which Meta accepts - and it may undo a STOP,
    because the member is the one choosing.
    """

    permission_classes = [IsAuthenticated]
    success_message = 'WhatsApp preference saved.'

    def put(self, request, conversation_id):
        conversation = _my_conversation(request, conversation_id)
        serializer = ConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        me = _my_membership(conversation, request.user)

        if serializer.validated_data['enabled']:
            if GroupChannel.whatsapp_for(conversation) is None:
                raise ConnectionRefused('This group is not on WhatsApp.', code='not_attached')
            if outbound.wa_id_for(me) is None:
                raise ConnectionRefused(
                    'Verify your phone number first.', code='no_verified_number'
                )
            answer = ChannelConsent.Status.OPTED_IN
        else:
            answer = ChannelConsent.Status.DECLINED

        membership.set_channel_consent(me, WHATSAPP, answer)
        return Response(_group_payload(conversation, request.user))


class GroupInviteView(APIView):
    """ "Invite by WhatsApp". Attaches the admin's number first if it needs to.

    An admin with no connected number gets reason `whatsapp_not_connected`, the
    frontend's cue to run Embedded Signup and then retry.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [WhatsAppInviteThrottle]
    success_message = 'WhatsApp invitation sent.'

    def post(self, request, conversation_id):
        conversation = _my_conversation(request, conversation_id)
        serializer = InviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        participant = None
        if data.get('participant'):
            participant = get_object_or_404(
                Participant.objects.select_related('user__profile'),
                conversation=conversation, pk=data['participant'],
            )
            # Inviting an existing member is asking them about a channel, which
            # is the group's to decide: admins only.
            if not conversation.is_admin(request.user):
                raise ConnectionRefused('Only a group admin can do that.', code='not_admin')

        result = outbound.invite(
            conversation,
            request.user,
            phone_number=data.get('phone_number'),
            display_name=(data.get('display_name') or '').strip(),
            participant=participant,
            connection_id=data.get('connection'),
        )
        body = {
            'outcome': result.outcome,
            'participant': result.participant.pk,
            'channel': GroupChannelSerializer(result.channel).data,
            'delivery_status': result.delivery.status if result.delivery else None,
            'reason': result.delivery.reason if result.delivery else '',
        }
        response = Response(body, status=status.HTTP_201_CREATED)
        response.message = {
            'invited': 'WhatsApp invitation sent.',
            'already_opted_in': 'They already get this group on WhatsApp.',
            'declined': 'They have opted out of WhatsApp from this business.',
            'not_sent': 'Added, but the WhatsApp invitation could not be sent.',
        }[result.outcome]
        return response
