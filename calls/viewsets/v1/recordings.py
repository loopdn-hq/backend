"""Recordings by their own id.

    GET    /api/v1/recordings/<id>/    metadata, and a short-lived download link
                                       once it is ready
    DELETE /api/v1/recordings/<id>/    delete the file (the record stays)

Listing and starting them is under the call - see calls/viewsets/v1/calls.py.
"""
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from calls import services
from calls.models import CallRecording
from calls.serializers import RecordingSerializer
from chat.models import Participant


class RecordingViewSet(
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = RecordingSerializer

    success_messages = {
        'retrieve': 'Recording fetched.',
        'destroy': 'Recording deleted.',
    }

    def get_queryset(self):
        return (
            CallRecording.objects.filter(
                call_room__conversation__participants__user=self.request.user,
                call_room__conversation__participants__status=Participant.Status.ACTIVE,
            )
            .select_related(
                'call_room__conversation', 'call_room__created_by', 'started_by__profile'
            )
            .distinct()
        )

    def get_object(self):
        recording = super().get_object()
        # Being in the conversation is not enough to hear a recording of it -
        # see services.may_view_recordings. Same 404 as a recording that does
        # not exist, so it cannot be probed for.
        if not services.may_view_recordings(self.request.user, recording.call_room):
            raise NotFound()
        return recording

    def retrieve(self, request, *args, **kwargs):
        recording = self.get_object()
        data = dict(self.get_serializer(recording).data)
        # Minted per request, for somebody who has just passed the check above,
        # and only when there is a file to download.
        data['download_url'] = (
            services.recording_download_url(recording)
            if recording.status == CallRecording.Status.COMPLETED
            else None
        )
        return Response(data)

    def destroy(self, request, *args, **kwargs):
        recording = self.get_object()
        if not services.may_administer(request.user, recording.call_room):
            raise PermissionDenied(
                'Only the person who started the call or a group admin can '
                'delete its recordings.'
            )
        services.delete_recording(request.user, recording)
        return Response(status=status.HTTP_204_NO_CONTENT)
