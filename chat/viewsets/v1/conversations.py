"""Version 1 of the chat API.

    GET  /api/v1/chat/conversations/               my conversations
    POST /api/v1/chat/conversations/direct/        start/find a 1-to-1 chat
    POST /api/v1/chat/conversations/group/         create a group (I am admin)
    GET  /api/v1/chat/conversations/<id>/          one conversation
    POST /api/v1/chat/conversations/<id>/members/  admin adds people
    GET  /api/v1/chat/conversations/<id>/messages/ history, newest first
    POST /api/v1/chat/conversations/<id>/messages/ send text and/or files

Access control is the queryset, not a check bolted on afterwards: every
lookup starts from the conversations the caller participates in, so a
non-member gets a 404 from every route here and cannot even learn that a
conversation exists.
"""
from django.db.models import Prefetch
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from chat.models import Conversation, Message, Participant
from chat.serializers import (
    AddMembersSerializer,
    ConversationSerializer,
    DirectConversationSerializer,
    GroupCreateSerializer,
    MessageCreateSerializer,
    MessageSerializer,
)


class ConversationViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated]
    serializer_class = ConversationSerializer

    serializer_classes = {
        'direct': DirectConversationSerializer,
        'group': GroupCreateSerializer,
        'members': AddMembersSerializer,
    }

    success_messages = {
        'list': 'Conversations fetched.',
        'retrieve': 'Conversation fetched.',
        'direct': 'Conversation ready.',
        'group': 'Group created.',
        'members': 'Members added.',
        'messages:GET': 'Messages fetched.',
        'messages:POST': 'Message sent.',
    }

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.serializer_class)

    def get_queryset(self):
        return (
            Conversation.objects.filter(participants__user=self.request.user)
            .distinct()
            .prefetch_related(
                Prefetch(
                    'participants',
                    queryset=Participant.objects.select_related('user__profile'),
                ),
                # One sliced prefetch instead of a query per row: the
                # conversation list needs the latest message of each.
                Prefetch(
                    'messages',
                    queryset=Message.objects.select_related('sender__profile')
                    .prefetch_related('attachments')
                    .order_by('-created_at')[:1],
                    to_attr='latest_messages',
                ),
            )
        )

    # -- starting conversations -------------------------------------------

    @action(detail=False, methods=['post'])
    def direct(self, request):
        """Open the one-to-one conversation with someone, creating it once.

        Safe to call every time the user taps a name: the same pair always
        resolves to the same conversation.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = serializer.save()
        return Response(self._as_conversation(conversation))

    @action(detail=False, methods=['post'])
    def group(self, request):
        """Create a group. The caller becomes its admin."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = serializer.save()
        return Response(
            self._as_conversation(conversation), status=status.HTTP_201_CREATED
        )

    # -- membership --------------------------------------------------------

    @action(detail=True, methods=['post'])
    def members(self, request, pk=None):
        """Add people to a group by username or phone number. Admins only."""
        conversation = self.get_object()

        if conversation.kind != Conversation.Kind.GROUP:
            raise ValidationError('Only groups can take extra members.')

        if not conversation.is_admin(request.user):
            raise PermissionDenied('Only a group admin can add people.')

        serializer = self.get_serializer(
            data=request.data,
            context={**self.get_serializer_context(), 'conversation': conversation},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # get_object() prefetched the participants before the new ones
        # existed, so serialising `conversation` now would return the old
        # list from that cache. Re-fetch to rebuild it.
        conversation = self.get_queryset().get(pk=conversation.pk)
        return Response(self._as_conversation(conversation))

    # -- messages ----------------------------------------------------------

    @action(
        detail=True,
        methods=['get', 'post'],
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def messages(self, request, pk=None):
        """Read the history, or post to it.

        get_object() is what enforces membership: a non-participant never
        reaches either branch.
        """
        conversation = self.get_object()

        if request.method == 'POST':
            return self._send_message(request, conversation)

        history = (
            conversation.messages.select_related('sender__profile')
            .prefetch_related('attachments')
        )
        page = self.paginate_queryset(history)
        serializer = MessageSerializer(
            page if page is not None else history,
            many=True,
            context=self.get_serializer_context(),
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def _send_message(self, request, conversation):
        serializer = MessageCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        message = serializer.save(conversation=conversation, sender=request.user)

        # create_message has already broadcast this to everyone's socket;
        # the sender gets it here as well so a plain REST client works with
        # no websocket at all.
        return Response(
            MessageSerializer(message, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def _as_conversation(self, conversation):
        return ConversationSerializer(
            conversation, context=self.get_serializer_context()
        ).data
