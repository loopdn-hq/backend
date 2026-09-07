"""Version 1 of the chat API.

    GET  /api/v1/chat/conversations/               my conversations
    POST /api/v1/chat/conversations/direct/        start/find a 1-to-1 chat
    POST /api/v1/chat/conversations/group/         create a group (I am admin)
                                                   name, and optionally a
                                                   description and an image
    GET  /api/v1/chat/conversations/<id>/          one conversation
    PATCH /api/v1/chat/conversations/<id>/settings/ admin edits the name,
                                                   description or image
    POST /api/v1/chat/conversations/<id>/members/  admin adds people
    POST /api/v1/chat/conversations/join/          join with a group code
    POST /api/v1/chat/conversations/<id>/leave/    take myself out
    GET  /api/v1/chat/conversations/<id>/requests/ admin: who is waiting
    POST /api/v1/chat/conversations/<id>/requests/ admin: approve or decline
    POST /api/v1/chat/conversations/<id>/remove/   admin removes or blocks
    POST /api/v1/chat/conversations/<id>/delivery/ mute it, or turn its texts off
    POST /api/v1/chat/conversations/<id>/role/     admin promotes or demotes
    POST /api/v1/chat/conversations/<id>/rename/   admin names a phone-only member
    POST /api/v1/chat/conversations/<id>/invite/   admin invites somebody by number
    DELETE /api/v1/chat/conversations/<id>/        admin deletes it, number and all
    GET  /api/v1/chat/conversations/<id>/join-code/ admin reads the code
    POST /api/v1/chat/conversations/<id>/join-code/ admin issues a new one
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
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rest_framework.pagination import PageNumberPagination

from chat import membership
from chat.models import Conversation, Message, Participant
from chat.serializers import (
    ADMIN_ONLY_SETTINGS,
    AddMembersSerializer,
    ConversationSerializer,
    ConversationSearchSerializer,
    DirectConversationSerializer,
    GroupCreateSerializer,
    GroupSettingsSerializer,
    InviteByPhoneSerializer,
    JoinGroupSerializer,
    JoinRequestDecisionSerializer,
    MessageCreateSerializer,
    MessageSerializer,
    DeliveryPreferenceSerializer,
    ParticipantRoleSerializer,
    RenameMemberSerializer,
    ParticipantSerializer,
    RemoveMemberSerializer,
)


class ParticipantPagination(PageNumberPagination):
    """Ten members a page, because that is roughly one screen.

    Its own class rather than the project default of 20: this list is rendered
    as rows in a sheet a user scrolls, where the first page wants to be small
    and fast, unlike a message history where 20 is one scrollback.

    `page_size_query_param` lets a client that knows better ask for more - an
    admin screen showing a whole group, say - and `max_page_size` stops that
    becoming a way to ask the server for an unbounded serialization.
    """

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class ConversationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = ConversationSerializer

    serializer_classes = {
        'direct': DirectConversationSerializer,
        'group': GroupCreateSerializer,
        'group_settings': GroupSettingsSerializer,
        'members': AddMembersSerializer,
        'join': JoinGroupSerializer,
        'requests': JoinRequestDecisionSerializer,
        'remove': RemoveMemberSerializer,
        'delivery': DeliveryPreferenceSerializer,
        'role': ParticipantRoleSerializer,
        'rename': RenameMemberSerializer,
        'invite': InviteByPhoneSerializer,
        'search': ConversationSearchSerializer,
    }

    success_messages = {
        'list': 'Conversations fetched.',
        'retrieve': 'Conversation fetched.',
        'search': 'Conversation search complete.',
        'direct': 'Conversation ready.',
        'group': 'Group created.',
        'group_settings': 'Group updated.',
        'members': 'Members added.',
        'participants': 'Participants fetched.',
        'messages:GET': 'Messages fetched.',
        'messages:POST': 'Message sent.',
        'join': 'Join request handled.',
        'leave': 'You have left the conversation.',
        'requests:GET': 'Pending requests fetched.',
        'requests:POST': 'Request handled.',
        'remove': 'Member removed.',
        'delivery': 'Delivery settings saved.',
        'role': 'Role updated.',
        'rename': 'Member renamed.',
        'invite': 'Invitation sent.',
        'destroy': 'Group deleted.',
        'join_code:GET': 'Join code fetched.',
        'join_code:POST': 'New join code issued.',
    }

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.serializer_class)

    def get_queryset(self):
        return (
            Conversation.objects.filter(
                participants__user=self.request.user,
                # The whole access-control model in one clause. Without it a
                # removed, departed or merely pending member still reads every
                # conversation they were ever in.
                participants__status=Participant.Status.ACTIVE,
            )
            .distinct()
            # A reverse one-to-one, so it joins rather than costing a query per
            # conversation - which it did, every time the list serialised the
            # group's number.
            # `created_by` is serialized on every row, and its avatar lives on
            # the profile - without both joins that is two queries per
            # conversation on the list.
            .select_related('sms_number', 'created_by__profile')
            .prefetch_related(
                Prefetch(
                    'participants',
                    # The member list a client renders is who is *in* the
                    # group, not everyone who ever was.
                    queryset=Participant.objects.filter(
                        status=Participant.Status.ACTIVE
                    ).select_related('user__profile'),
                ),
                # One sliced prefetch instead of a query per row: the
                # conversation list needs the latest message of each.
                Prefetch(
                    'messages',
                    queryset=Message.objects.select_related(
                        'sender__profile', 'sender_participant__user__profile'
                    )
                    .prefetch_related('attachments')
                    .order_by('-created_at')[:1],
                    to_attr='latest_messages',
                ),
            )
        )

    # -- starting conversations -------------------------------------------

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Find named groups that can be discovered and joined."""
        term = (request.query_params.get('q') or '').strip()
        matches = Conversation.objects.filter(
            kind=Conversation.Kind.GROUP,
            name__icontains=term,
        ).exclude(
            join_mode=Conversation.JoinMode.INVITE,
        ).order_by('name', 'id')

        if len(term) < 2:
            matches = matches.none()

        page = self.paginate_queryset(matches)
        serializer = self.get_serializer(
            page if page is not None else matches,
            many=True,
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

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

    @action(
        detail=False,
        methods=['post'],
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def group(self, request):
        """Create a group. The caller becomes its admin.

        The parsers are DRF's own defaults, listed here for the same reason
        `messages` lists them: an endpoint that takes a file should say so
        where it is defined, rather than relying on a setting we do not set.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = serializer.save()
        return Response(
            self._as_conversation(conversation), status=status.HTTP_201_CREATED
        )

    @action(
        detail=True,
        methods=['patch'],
        url_path='settings',
        url_name='settings',
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def group_settings(self, request, pk=None):
        """Edit a group's name, description or image. Admins only.

        PATCH rather than PUT: a client changing the photo should not have to
        resend the name and description to keep them.

        Not called `settings`, tempting as the name is - APIView carries a
        class attribute of that name, which DRF reads on every request
        (`self.settings.FORMAT_SUFFIX_KWARG`, `self.settings.EXCEPTION_HANDLER`),
        so a method would shadow it and 500 every route on this viewset.
        """
        conversation = self.get_object()

        if conversation.kind != Conversation.Kind.GROUP:
            raise ValidationError('Only groups have settings to edit.')

        if not conversation.may_edit_group(request.user):
            raise PermissionDenied('You cannot edit this group.')

        # The policy settings stay with admins even where members may edit the
        # name and photo, or a member could simply grant themselves the rest.
        asked_for = set((request.data.get('settings') or {}))
        if asked_for & ADMIN_ONLY_SETTINGS and not conversation.is_admin(request.user):
            raise PermissionDenied('Only a group admin can change group settings.')

        serializer = self.get_serializer(conversation, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        conversation = serializer.save()

        # No re-fetch, unlike `members`: this writes the conversation row
        # itself, so get_object()'s participant and message prefetches are
        # still current.
        return Response(self._as_conversation(conversation))

    # -- membership --------------------------------------------------------

    @action(detail=True, methods=['get'], pagination_class=ParticipantPagination)
    def participants(self, request, pk=None):
        """Who is in this conversation, ten at a time.

        Its own endpoint rather than a field on every conversation reply,
        because the member list is unbounded and each row carries a nested user
        and profile: a group of two hundred made every fetch of the group carry
        two hundred profiles.

        ACTIVE rows only - the same set the conversation payload carries.
        Somebody who left, was removed or is waiting on approval is not a
        member; pending requests have their own endpoint (`requests`).

        Ordered by `joined_at` **then `pk`**. Paginating on a non-unique sort
        key can show one row on two pages and skip another entirely, and
        `joined_at` is not unique - everybody added in a single request shares
        a timestamp to the microsecond.

        Access control is `get_object()`, which runs the member-scoped
        queryset, so a non-member gets 404 here exactly as everywhere else.
        """
        conversation = self.get_object()

        rows = (
            Participant.objects.filter(
                conversation=conversation, status=Participant.Status.ACTIVE
            )
            .select_related('user__profile')
            .order_by('joined_at', 'pk')
        )

        page = self.paginate_queryset(rows)
        serializer = ParticipantSerializer(
            page if page is not None else rows,
            many=True,
            context=self.get_serializer_context(),
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def members(self, request, pk=None):
        """Add people to a group by username or phone number. Admins only."""
        conversation = self.get_object()

        if conversation.kind != Conversation.Kind.GROUP:
            raise ValidationError('Only groups can take extra members.')

        if not conversation.may_add_members(request.user):
            raise PermissionDenied('You cannot add people to this group.')

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

    @action(detail=False, methods=['post'])
    def join(self, request):
        """Join a group with the code from its shareable link.

        AllowAny is deliberately NOT used - joining still requires an account.
        What makes this route different is that it does not go through
        `get_object()`: by definition the caller is not yet a member, so the
        access-control queryset would 404 them.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        participant = serializer.save()

        conversation = self.get_queryset().filter(
            pk=participant.conversation_id
        ).first()
        # PENDING members are not in `get_queryset` - they are not members yet -
        # so there is no conversation to hand back, only the fact that they
        # asked.
        if conversation is None:
            return Response({'status': participant.status})
        return Response(self._as_conversation(conversation))

    @action(detail=True, methods=['post'], url_path='join', url_name='join-open')
    def join_open(self, request, pk=None):
        """Join an open group or request access to an approval group."""
        conversation = Conversation.objects.filter(
            pk=pk, kind=Conversation.Kind.GROUP
        ).first()
        if conversation is None:
            raise NotFound('Conversation not found.')
        if conversation.join_mode not in (
            Conversation.JoinMode.OPEN,
            Conversation.JoinMode.APPROVAL,
        ):
            raise ValidationError(
                'This conversation is not open to joining or requesting access.'
            )

        participant = membership.request_to_join(conversation, user=request.user)
        if participant.status == Participant.Status.PENDING:
            return Response({'status': participant.status})

        conversation = self.get_queryset().get(pk=conversation.pk)
        return Response(self._as_conversation(conversation))

    @action(detail=True, methods=['post'])
    def leave(self, request, pk=None):
        """Take myself out of this conversation."""
        conversation = self.get_object()
        participant = Participant.objects.get(
            conversation=conversation, user=request.user
        )
        membership.leave(participant)
        return Response({'status': participant.status})

    @action(detail=True, methods=['get', 'post'])
    def requests(self, request, pk=None):
        """List people waiting to be let in, or approve/decline one."""
        conversation = self.get_object()

        if not conversation.is_admin(request.user):
            raise PermissionDenied('Only a group admin can review requests.')

        if request.method == 'GET':
            # Participant.objects, not conversation.participants: the
            # queryset above prefetches only ACTIVE rows, and a related manager
            # filters that cache, so the relation would always come back empty.
            pending = Participant.objects.filter(
                conversation=conversation, status=Participant.Status.PENDING
            ).select_related('user__profile')
            return Response(ParticipantSerializer(pending, many=True).data)

        serializer = self.get_serializer(
            data=request.data,
            context={**self.get_serializer_context(), 'conversation': conversation},
        )
        serializer.is_valid(raise_exception=True)

        decide = (
            membership.approve
            if serializer.validated_data['decision'] == 'approve'
            else membership.decline
        )
        participant = decide(serializer.participant, request.user)
        return Response(ParticipantSerializer(participant).data)

    @action(detail=True, methods=['post'])
    def remove(self, request, pk=None):
        """Take somebody out of a group, optionally blocking them for good."""
        conversation = self.get_object()
        serializer = self.get_serializer(
            data=request.data,
            context={**self.get_serializer_context(), 'conversation': conversation},
        )
        serializer.is_valid(raise_exception=True)

        participant = membership.remove(
            serializer.participant,
            request.user,
            block=serializer.validated_data['block'],
        )
        return Response(ParticipantSerializer(participant).data)

    @action(detail=True, methods=['post'])
    def delivery(self, request, pk=None):
        """Change how this conversation reaches me.

        `muted` silences it everywhere; `sms_enabled` decides whether texts are
        one of its channels for me. Both are mine alone - an admin cannot set
        them for somebody else, which is why this takes no participant id.
        """
        conversation = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        participant = Participant.objects.get(
            conversation=conversation, user=request.user
        )
        membership.set_delivery(participant, **serializer.validated_data)
        return Response(ParticipantSerializer(participant).data)

    @action(detail=True, methods=['post'])
    def role(self, request, pk=None):
        """Promote a member to admin, or demote them. Admins only.

        This is also the way out for a group's only admin: `leave` and `remove`
        both refuse to take the last one, so without a handover the founder is
        stuck in the group permanently.
        """
        conversation = self.get_object()
        serializer = self.get_serializer(
            data=request.data,
            context={**self.get_serializer_context(), 'conversation': conversation},
        )
        serializer.is_valid(raise_exception=True)

        participant = membership.set_role(
            serializer.participant, serializer.validated_data['role'], request.user
        )
        return Response(ParticipantSerializer(participant).data)

    @action(detail=True, methods=['get', 'post'], url_path='join-code',
            url_name='join-code')
    def join_code(self, request, pk=None):
        """Read the group's join code, or issue a new one. Admins only.

        Kept off `ConversationSerializer` on purpose. The code is the thing that
        lets a stranger in, so handing it to every member on every conversation
        fetch would put it in far more logs, caches and screenshots than the
        people who actually administer the group.

        POST is how a leaked code is revoked: there is no separate delete,
        because a group with no code and a mode that needs one is just broken.
        """
        conversation = self.get_object()

        if conversation.kind != Conversation.Kind.GROUP:
            raise ValidationError('Only groups have a join code.')

        if not conversation.is_admin(request.user):
            raise PermissionDenied('Only a group admin can see the join code.')

        if request.method == 'POST':
            conversation.regenerate_join_code()

        return Response(
            {'join_mode': conversation.join_mode, 'join_code': conversation.join_code}
        )

    @action(detail=True, methods=['post'])
    def rename(self, request, pk=None):
        """Give a member with no account a name. Admins only.

        Somebody who joined by texting JOIN never gave one, so the group sees a
        masked number until an admin puts a name to it.
        """
        conversation = self.get_object()

        if not conversation.is_admin(request.user):
            raise PermissionDenied('Only a group admin can rename a member.')

        serializer = self.get_serializer(
            data=request.data,
            context={**self.get_serializer_context(), 'conversation': conversation},
        )
        serializer.is_valid(raise_exception=True)

        participant = serializer.participant
        participant.display_name = serializer.validated_data['display_name']
        participant.save(update_fields=['display_name'])
        return Response(ParticipantSerializer(participant).data)

    @action(detail=True, methods=['post'])
    def invite(self, request, pk=None):
        """Invite somebody to a group by phone number. Admins only.

        The way into an Invite Only group, and the only way to add somebody who
        has no account. They are texted an invitation and receive nothing from
        the group until they reply YES.
        """
        conversation = self.get_object()
        if not conversation.may_add_members(request.user):
            raise PermissionDenied('You cannot add people to this group.')

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        participant = membership.invite_by_phone(
            conversation,
            serializer.validated_data['phone_number'],
            serializer.validated_data['display_name'],
            request.user,
        )
        return Response(
            ParticipantSerializer(participant).data, status=status.HTTP_201_CREATED
        )

    def destroy(self, request, *args, **kwargs):
        """Delete a group. Admins only, and it takes the number with it.

        The number is released by a pre_delete signal rather than here, so that
        it happens however the group is deleted - this endpoint, the admin, or a
        management command. A number that outlives its group is one nobody is
        watching and everybody is still paying for.
        """
        conversation = self.get_object()

        if conversation.kind != Conversation.Kind.GROUP:
            # A one-to-one conversation belongs to two people equally; there is
            # nobody with the standing to delete it for both of them.
            raise ValidationError('Only groups can be deleted.')

        if not conversation.is_admin(request.user):
            raise PermissionDenied('Only a group admin can delete the group.')

        conversation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

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

        # A resume cursor. Without one, a client that reconnects cannot ask
        # "what did I miss" - it can only re-page the newest-first history and
        # diff, which is unbounded and gets worse the longer it was away.
        #
        # Keyed on `id`, not `created_at`: created_at is app-server wall clock,
        # so two tasks can stamp messages out of order, and it has no tiebreak.
        # Ids come from one sequence and are a sound total order.
        after_id = request.query_params.get('after_id')

        history = (
            conversation.messages.select_related(
                'sender__profile',
                # Messages that arrived by SMS have no `sender`, and the
                # serializer falls back to the membership row for the name.
                # Without this join that is one extra query per such message.
                'sender_participant__user__profile',
            )
            .prefetch_related('attachments')
        )

        if after_id is not None:
            try:
                cursor = int(after_id)
            except (TypeError, ValueError):
                raise ValidationError('after_id must be a whole number.')
            # ASCENDING here, unlike the default history. A client catching up
            # appends in the order things happened; handing it newest-first
            # would make it reverse every page before rendering.
            history = history.filter(pk__gt=cursor).order_by('pk')

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
