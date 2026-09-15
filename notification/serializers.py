"""Serializers for the activity feed."""
from rest_framework import serializers

from chat.models import Participant
from notification.models import Notification
from user.serializers import PublicUserSerializer


class NotificationSerializer(serializers.ModelSerializer):
    """One row of somebody's activity feed.

    `conversation_name` is denormalized onto the response rather than nesting
    the whole conversation: the feed renders a group's name under every row,
    and nesting would put a full conversation - settings, sms_number,
    last_message - behind each of them.

    The text itself is NOT stored or sent. A row says what happened and what it
    happened to; the client renders the sentence. Store "Alice added you to
    Family" and renaming the group leaves it wrong forever.
    """

    from_user = PublicUserSerializer(read_only=True)
    category = serializers.CharField(read_only=True)
    conversation_name = serializers.SerializerMethodField()
    conversation_join_mode = serializers.SerializerMethodField()
    viewer_status = serializers.SerializerMethodField()
    request_status = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            'id',
            'type',
            'category',
            'from_user',
            'conversation',
            'conversation_name',
            'conversation_join_mode',
            'viewer_status',
            'request_status',
            'message',
            'data',
            'read_at',
            'created_at',
        ]
        read_only_fields = fields

    def get_conversation_name(self, obj):
        conversation = obj.conversation
        if conversation is None:
            return None
        return conversation.name or ''

    def get_conversation_join_mode(self, obj):
        """How somebody gets into this group.

        Context for a client deciding what to offer: an open group can be
        joined outright, a code group needs the code, an approval group is a
        request, and an invite-only group cannot be asked at all.
        """
        conversation = obj.conversation
        return conversation.join_mode if conversation else None

    def get_viewer_status(self, obj):
        """Where the RECIPIENT stands in that conversation right now.

        Computed live rather than recorded with the event, because the
        notification is a record of the past and the action has to be about the
        present. Somebody added and then removed must not still be offered a
        way in, and somebody who has since joined must not be offered the join
        form again.

        None means no membership row at all.
        """
        if obj.conversation_id is None:
            return None
        return self._status_map().get(obj.conversation_id)

    def get_request_status(self, obj):
        """For a join request: where THAT request stands now.

        A notification is a record, not a queue - the request it announced may
        have been approved from the admin panel, declined by another admin, or
        answered here a moment ago. Without this the client can only offer
        Approve and Decline forever and find out it was too late by failing.

        None when the row is not a join request, or when the membership it
        pointed at has since been deleted.
        """
        if obj.type != Notification.Type.JOIN_REQUESTED:
            return None
        participant_id = (obj.data or {}).get('participant_id')
        if participant_id is None:
            return None
        return self._request_status_map().get(participant_id)

    def _request_status_map(self):
        """The status of every membership this page's requests point at.

        One query for the page, cached on the shared serializer context, so a
        feed of twenty requests does not become twenty lookups.
        """
        cached = self.context.get('_request_statuses')
        if cached is not None:
            return cached

        ids = {
            (notification.data or {}).get('participant_id')
            for notification in self.context.get('_page_notifications', [])
            if notification.type == Notification.Type.JOIN_REQUESTED
        }
        ids.discard(None)

        mapping = (
            dict(
                Participant.objects.filter(pk__in=ids).values_list('pk', 'status')
            )
            if ids
            else {}
        )
        self.context['_request_statuses'] = mapping
        return mapping

    def _status_map(self):
        """Every membership this viewer has, in one query for the whole page.

        Cached on the serializer context, which is shared across the page, so
        a feed of twenty notifications about twenty groups still costs one
        query rather than twenty.
        """
        cached = self.context.get('_viewer_participation')
        if cached is not None:
            return cached

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            self.context['_viewer_participation'] = {}
            return {}

        rows = Participant.objects.filter(user=user).values_list(
            'conversation_id', 'status'
        )
        mapping = dict(rows)
        self.context['_viewer_participation'] = mapping
        return mapping


class MarkNotificationsReadSerializer(serializers.Serializer):
    """Which notifications to mark read.

    An explicit list marks those; omitting it marks everything the caller has
    unread, which is what the "Mark all as read" control means.
    """

    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False,
        allow_empty=True,
    )
