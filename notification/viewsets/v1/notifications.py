"""Version 1 of the activity API.

    GET  /api/v1/chat/notifications/             my activity, newest first
    POST /api/v1/chat/notifications/read/        mark some, or all, as read

Access control is the queryset, as it is for conversations: every lookup
starts from the rows addressed to the caller, so there is no route here that
can reach somebody else's activity.
"""
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from notification.models import Notification
from notification.serializers import (
    MarkNotificationsReadSerializer,
    NotificationSerializer,
)


class NotificationPagination(PageNumberPagination):
    """Twenty a page, matching the project default.

    The feed is read top-down and rarely scrolled far, so the page size that
    matters is the first one.
    """

    page_size_query_param = 'page_size'
    max_page_size = 100


class NotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer
    pagination_class = NotificationPagination

    serializer_classes = {
        'read': MarkNotificationsReadSerializer,
    }

    success_messages = {
        'list': 'Activity fetched.',
        'read': 'Marked as read.',
    }

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.serializer_class)

    def get_serializer(self, *args, **kwargs):
        """Hand the serializer the whole page, not just one row at a time.

        A join request's current status lives on the membership its `data`
        points at, and resolving that per row would be a query per
        notification. Materialising the page here lets the serializer look
        them all up once.

        Set before delegating, because get_serializer_context runs inside the
        call below.
        """
        if kwargs.get('many') and args:
            self._page_rows = list(args[0])
            args = (self._page_rows,) + args[1:]
        return super().get_serializer(*args, **kwargs)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['_page_notifications'] = getattr(self, '_page_rows', [])
        return context

    def get_queryset(self):
        queryset = (
            Notification.objects.filter(to_user=self.request.user)
            # from_user's avatar lives on the profile and the feed renders one
            # per row; without both joins that is two queries per notification.
            .select_related('from_user__profile', 'conversation')
        )

        # The client's filter chips. `category` is derived from the type's dot
        # prefix, so filtering translates to a prefix match rather than a
        # second mapping of types to chips that could drift from the first.
        category = self.request.query_params.get('category')
        if category:
            prefixes = [
                prefix
                for prefix, name in Notification.CATEGORY_BY_PREFIX.items()
                if name == category
            ]
            if prefixes:
                clause = Q()
                for prefix in prefixes:
                    clause |= Q(type__startswith=f'{prefix}.')
                queryset = queryset.filter(clause)
            else:
                queryset = queryset.none()

        if self.request.query_params.get('unread') in ('1', 'true', 'True'):
            queryset = queryset.filter(read_at__isnull=True)

        return queryset

    def list(self, request, *args, **kwargs):
        """The feed, plus the unread total.

        The count rides along because every client that renders this list also
        renders a badge for it, and asking for both separately means two round
        trips that can disagree by the time the second one lands.
        """
        response = super().list(request, *args, **kwargs)
        unread = Notification.objects.filter(
            to_user=request.user, read_at__isnull=True
        ).count()
        if isinstance(response.data, dict):
            response.data['unread_count'] = unread
        return response

    @action(detail=False, methods=['post'])
    def read(self, request):
        """Mark some notifications read, or all of them.

        Scoped to the caller's own rows by the queryset, so a stray id from
        somebody else's feed matches nothing rather than marking their row.
        Already-read rows are left alone: re-stamping them would move an event
        the user dealt with last week to the top of a "recently read" list.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data.get('ids')

        unread = self.get_queryset().filter(read_at__isnull=True)
        if ids:
            unread = unread.filter(pk__in=ids)

        marked = unread.update(read_at=timezone.now())
        remaining = Notification.objects.filter(
            to_user=request.user, read_at__isnull=True
        ).count()
        return Response({'marked': marked, 'unread_count': remaining})
