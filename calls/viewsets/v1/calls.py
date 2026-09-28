"""Version 1 of the calls API.

    GET  /api/v1/calls/?conversation=<id>       calls I can see, newest first
    POST /api/v1/calls/                         start a call in a conversation
                                                and join it: returns a LiveKit
                                                token for the caller
    GET  /api/v1/calls/<id>/                    one call
    POST /api/v1/calls/<id>/join/               join it (or refresh my token)
    POST /api/v1/calls/<id>/leave/              leave it; the last one out ends it
    POST /api/v1/calls/<id>/reject/             turn down an incoming call
    POST /api/v1/calls/<id>/end/                end it for everyone
    POST /api/v1/calls/<id>/recording/start/    start recording
    POST /api/v1/calls/<id>/recording/stop/     stop recording
    GET  /api/v1/calls/<id>/recordings/         its recordings

Access control is the queryset, as in chat: every lookup starts from the calls of
conversations the caller is an active member of, so a call anywhere else is a
404 from every route here and nobody can learn that it exists. The services
check membership again under the call's row lock, because the answer can change
between the two.

Where these are mounted, and the recording routes that are not under a call, are
in calls/urls.py.
"""
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from calls import services
from calls.models import CallRoom
from calls.serializers import CallSerializer, CallStartSerializer, RecordingSerializer
from calls.throttles import (
    CallJoinRateThrottle,
    CallRecordingRateThrottle,
    CallStartRateThrottle,
)
from chat.models import Conversation, Participant


class CallViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = CallSerializer

    success_messages = {
        'list': 'Calls fetched.',
        'retrieve': 'Call fetched.',
        'create': 'Call started.',
        'join': 'Joined the call.',
        'leave': 'You have left the call.',
        'reject': 'Call declined.',
        'end': 'Call ended.',
        'recording_start': 'Recording started.',
        'recording_stop': 'Recording stopped.',
        'recordings': 'Recordings fetched.',
    }

    # Which actions are rate limited, and by what.
    throttles_by_action = {
        'create': [CallStartRateThrottle],
        'join': [CallJoinRateThrottle],
        'recording_start': [CallRecordingRateThrottle],
    }

    def get_throttles(self):
        classes = self.throttles_by_action.get(self.action, [])
        return [throttle() for throttle in classes]

    def get_queryset(self):
        queryset = services.calls_visible_to(self.request.user)

        if self.action == 'list':
            conversation = self.request.query_params.get('conversation')
            if conversation:
                if not conversation.isdigit():
                    raise NotFound('No such conversation.')
                queryset = queryset.filter(conversation_id=int(conversation))
            state = self.request.query_params.get('status')
            if state in CallRoom.Status.values:
                queryset = queryset.filter(status=state)
        return queryset

    def _render(self, call_id, **extra):
        """The call as it is NOW, after a service changed it."""
        call = services.call_queryset().get(pk=call_id)
        data = CallSerializer(call, context={'request': self.request}).data
        return {'call': data, **extra}

    @staticmethod
    def _connection(connection):
        return {'livekit': connection}

    # -- start / join --------------------------------------------------------

    def create(self, request, *args, **kwargs):
        serializer = CallStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Resolved from the caller's own conversations, so somebody else's is
        # the same 404 as one that does not exist.
        conversation = get_object_or_404(
            Conversation.objects.filter(
                participants__user=request.user,
                participants__status=Participant.Status.ACTIVE,
            ).distinct(),
            pk=serializer.validated_data['conversation'],
        )

        call, connection = services.start_call(
            request.user, conversation, serializer.validated_data['call_type']
        )
        return Response(
            self._render(call.pk, **self._connection(connection)),
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'])
    def join(self, request, pk=None):
        call = self.get_object()
        call, connection = services.join_call(request.user, call.pk)
        return Response(self._render(call.pk, **self._connection(connection)))

    # -- leaving -------------------------------------------------------------

    @action(detail=True, methods=['post'])
    def leave(self, request, pk=None):
        call = self.get_object()
        services.leave_call(request.user, call.pk)
        return Response(self._render(call.pk))

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        call = self.get_object()
        services.reject_call(request.user, call.pk)
        return Response(self._render(call.pk))

    @action(detail=True, methods=['post'])
    def end(self, request, pk=None):
        call = self.get_object()
        services.end_call(request.user, call.pk)
        return Response(self._render(call.pk))

    # -- recording -----------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='recording/start')
    def recording_start(self, request, pk=None):
        call = self.get_object()
        recording = services.start_recording(request.user, call.pk)
        return Response(
            RecordingSerializer(recording, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='recording/stop')
    def recording_stop(self, request, pk=None):
        call = self.get_object()
        recording = services.stop_recording(request.user, call.pk)
        return Response(RecordingSerializer(recording, context={'request': request}).data)

    @action(detail=True, methods=['get'])
    def recordings(self, request, pk=None):
        call = self.get_object()
        # A member who was never on the call is told nothing, not "forbidden":
        # a 403 would confirm there is something to see.
        if not services.may_view_recordings(request.user, call):
            raise NotFound('No such call.')
        return Response(
            RecordingSerializer(
                call.recordings.select_related('started_by__profile'),
                many=True,
                context={'request': request},
            ).data
        )
