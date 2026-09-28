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
    POST /api/v1/chat/conversations/<id>/delivery/ mute it, or turn its texts
                                                   or emails off
    POST /api/v1/chat/conversations/<id>/role/     admin promotes or demotes
    POST /api/v1/chat/conversations/<id>/rename/   admin names a phone-only member
    POST /api/v1/chat/conversations/<id>/invite/   admin invites somebody by number
    DELETE /api/v1/chat/conversations/<id>/        admin deletes it, number and all
    GET  /api/v1/chat/conversations/<id>/join-code/ admin reads the code
    POST /api/v1/chat/conversations/<id>/join-code/ admin issues a new one
    GET  /api/v1/chat/conversations/<id>/messages/ history, newest first
    POST /api/v1/chat/conversations/<id>/messages/ send text and/or files,
                                                   reply to one, or forward one
    PATCH  .../<id>/messages/<mid>/              sender edits their own words
    DELETE .../<id>/messages/<mid>/              sender or admin deletes it
    POST   .../<id>/messages/<mid>/reactions/    add my emoji
    DELETE .../<id>/messages/<mid>/reactions/?emoji= remove it
    POST   .../<id>/messages/<mid>/pin/          admin pins it above the thread
    DELETE .../<id>/messages/<mid>/pin/          admin unpins it
    POST /api/v1/chat/conversations/<id>/read/     mark it read up to a message
    POST /api/v1/chat/conversations/<id>/archive/  tidy it out of my list
    POST /api/v1/chat/conversations/<id>/clear/    empty MY view of it

Access control is the queryset, not a check bolted on afterwards: every
lookup starts from the conversations the caller participates in, so a
non-member gets a 404 from every route here and cannot even learn that a
conversation exists.
"""
from django.db.models import (
    Count,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce, Greatest
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rest_framework.pagination import PageNumberPagination

from django.utils import timezone

from chat import membership
from chat.models import Conversation, Message, MessageReaction, Participant
from chat.serializers import (
    ADMIN_ONLY_SETTINGS,
    MESSAGE_PREFETCH_RELATED,
    MESSAGE_SELECT_RELATED,
    AddMembersSerializer,
    ArchiveSerializer,
    ConversationSerializer,
    ConversationSearchSerializer,
    DirectConversationSerializer,
    GroupCreateSerializer,
    GroupSettingsSerializer,
    InviteByPhoneSerializer,
    JoinGroupSerializer,
    JoinRequestDecisionSerializer,
    MarkReadSerializer,
    MessageCreateSerializer,
    MessageEditSerializer,
    MessageReactionSerializer,
    MessageSerializer,
    DeliveryPreferenceSerializer,
    ParticipantRoleSerializer,
    RenameMemberSerializer,
    ParticipantSerializer,
    NudgeMemberSerializer,
    RemoveMemberSerializer,
    member_candidates,
)
from chat.services import broadcast_conversation_updated, broadcast_message_updated


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
        'nudge': NudgeMemberSerializer,
        'delivery': DeliveryPreferenceSerializer,
        'read': MarkReadSerializer,
        'archive': ArchiveSerializer,
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
        'search_members': 'Search complete.',
        'nudge': 'Reminder sent.',
        'delivery': 'Delivery settings saved.',
        'read': 'Marked as read.',
        'archive': 'Conversation updated.',
        'clear': 'Conversation cleared.',
        'role': 'Role updated.',
        'rename': 'Member renamed.',
        'invite': 'Invitation sent.',
        'destroy': 'Group deleted.',
        'join_code:GET': 'Join code fetched.',
        'join_code:POST': 'New join code issued.',
    }

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.serializer_class)

    def unread_count_subquery(self):
        """How many messages the caller has not read, per conversation.

        Correlated so it costs one subquery in the SELECT rather than a query
        per row. Two levels of OuterRef because it is a subquery inside a
        subquery: the inner one reaches past the Message query it sits in, back
        out to the Conversation being counted.

        `exclude(sender=...)` is what stops your own messages counting against
        you. Coalesce covers a member who has read nothing - a NULL pointer
        would make every comparison NULL and report zero unread, which is the
        exact opposite of the truth.
        """
        user = self.request.user

        # clear_history moves the read pointer with the cutoff, so the read
        # pointer alone is enough here - but taking the greater of the two is
        # what keeps that true if either is ever moved on its own.
        read_pointer = Participant.objects.filter(
            conversation=OuterRef(OuterRef('pk')), user=user
        ).annotate(
            floor=Greatest(
                Coalesce('last_read_message_id', Value(0)),
                Coalesce('cleared_before_message_id', Value(0)),
            )
        ).values('floor')[:1]

        return (
            Message.objects.filter(conversation=OuterRef('pk'))
            .exclude(sender=user)
            .filter(pk__gt=Coalesce(Subquery(read_pointer), Value(0)))
            .order_by()
            .values('conversation')
            .annotate(total=Count('pk'))
            .values('total')
        )

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
            .select_related(
                'sms_number',
                'created_by__profile',
                # The pinned message is serialized in full on every row. One
                # join chain for the page, not a query per conversation.
                *(f'pinned_message__{path}' for path in MESSAGE_SELECT_RELATED),
            )
            .prefetch_related(
                *(f'pinned_message__{path}' for path in MESSAGE_PREFETCH_RELATED),
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
                        *MESSAGE_SELECT_RELATED
                    )
                    .prefetch_related(*MESSAGE_PREFETCH_RELATED)
                    .order_by('-created_at')[:1],
                    to_attr='latest_messages',
                ),
            )
            .annotate(
                unread_count=Coalesce(
                    Subquery(
                        self.unread_count_subquery(),
                        output_field=IntegerField(),
                    ),
                    Value(0),
                )
            )
        )

    # -- starting conversations -------------------------------------------

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Find named groups that can be discovered and joined.

        Three filters, and each one is the difference between a directory and
        a leak:

        INVITE MODE IS EXCLUDED. An invite-only group is not discoverable by
        definition - listing it would announce that it exists and who might be
        in it to somebody who can never join.

        GROUPS THE CALLER IS ALREADY IN ARE EXCLUDED. They are not results;
        they are already in the sidebar, and offering "Join" for a group
        somebody is reading is a button that can only confuse. Only ACTIVE
        membership counts - a pending request is not being in.

        A TERM OF UNDER TWO CHARACTERS RETURNS NOTHING, so this is a lookup
        rather than a way to page through every public group in the product.
        """
        term = (request.query_params.get('q') or '').strip()
        matches = (
            Conversation.objects.filter(
                kind=Conversation.Kind.GROUP,
                name__icontains=term,
            )
            .exclude(join_mode=Conversation.JoinMode.INVITE)
            # An explicit subquery of "conversations I am an active member of"
            # rather than .exclude(participants__user=..., participants__status=
            # ...). Django's exclude() spans a multi-valued relation with its own
            # join, and next to the annotate() below that quietly removed groups
            # whose only ACTIVE row belonged to somebody else. This form says
            # exactly what it means and cannot be read two ways.
            .exclude(
                pk__in=Participant.objects.filter(
                    user=request.user, status=Participant.Status.ACTIVE
                ).values('conversation_id')
            )
            # Annotated rather than counted per row: the card shows a member
            # count, and without this that is a query for every result.
            .annotate(
                active_member_count=Count(
                    'participants',
                    filter=Q(participants__status=Participant.Status.ACTIVE),
                    distinct=True,
                )
            )
            .order_by('name', 'id')
        )

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
            # privacy_settings too: the serializer asks each member whether
            # their read receipts may be shown, and without this that is a
            # query per row.
            .select_related('user__profile', 'user__privacy_settings')
            # Consent is read per channel for every row - `email_enabled` and
            # `channels` both walk it. Without this that is a query per member,
            # and it was one before `channels` existed.
            .prefetch_related('channel_consents')
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

    @action(
        detail=True, methods=['get'], url_path='members/search',
        url_name='search-members',
    )
    def search_members(self, request, pk=None):
        """Find people to add, and say what adding each one would do.

        READ-ONLY, and for whoever may add people - the same standing the add
        needs, since this is its first half. `member_candidates` has the rules:
        username by prefix, email and number exact, at most a handful at once.
        """
        conversation = self.get_object()
        if not conversation.may_add_members(request.user):
            raise PermissionDenied('You cannot add people to this group.')

        results, has_more, invite = member_candidates(
            conversation,
            request.query_params.get('q'),
            request.user,
            context=self.get_serializer_context(),
        )
        return Response(
            {'results': results, 'has_more': has_more, 'invite': invite}
        )

    @action(detail=True, methods=['post'])
    def nudge(self, request, pk=None):
        """Ask a member again about an invitation they never answered.

        ADMIN-TRIGGERED, never automatic, and at most once a day per channel.
        A nudge is a message to somebody who has not consented, which is the
        traffic carriers police hardest - so a person decides to send each one,
        and `membership.nudge` refuses anybody who has already answered.
        """
        conversation = self.get_object()
        serializer = self.get_serializer(
            data=request.data,
            context={**self.get_serializer_context(), 'conversation': conversation},
        )
        serializer.is_valid(raise_exception=True)

        membership.nudge(
            serializer.participant,
            serializer.validated_data['channel'],
            request.user,
        )
        # The participant, so a client can re-render the row it just acted on
        # with its new `reminded_at` and the refusal that now applies.
        return Response(
            ParticipantSerializer(
                serializer.participant, context=self.get_serializer_context()
            ).data
        )

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

        `muted` silences it everywhere; `sms_enabled` and `email_enabled` decide
        whether texts and emails are channels for me at all. All three are mine
        alone - an admin cannot set them for somebody else, which is why this
        takes no participant id.

        This is also the only door that turns email back ON. The unsubscribe
        link printed in every group email can only ever subtract, because a
        forwarded link must not let a stranger undo somebody's decision.
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
    def clear(self, request, pk=None):
        """Empty my view of this conversation, without leaving it.

        Mine alone. The member stays in the group, everybody else's copy is
        untouched, and nothing is destroyed - their own history simply starts
        after the newest message. The conversation leaves their list because
        there is nothing left to show, and returns by itself when somebody
        posts, carrying only what arrived after.
        """
        conversation = self.get_object()
        participant = Participant.objects.get(
            conversation=conversation, user=request.user
        )
        membership.clear_history(participant)

        conversation = self.get_queryset().get(pk=conversation.pk)
        return Response(self._as_conversation(conversation))

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        """Move this conversation out of my list, or back into it.

        Mine alone, like `delivery` and `read`, so it takes no participant id.
        It is not leaving and it is not muting: the member stays in the group
        and every channel keeps delivering, including SMS. Only which of their
        own lists it appears in changes.
        """
        conversation = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        participant = Participant.objects.get(
            conversation=conversation, user=request.user
        )
        membership.set_archived(participant, serializer.validated_data['archived'])

        conversation = self.get_queryset().get(pk=conversation.pk)
        return Response(self._as_conversation(conversation))

    @action(detail=True, methods=['post'])
    def read(self, request, pk=None):
        """Mark this conversation read, up to a message.

        Mine alone, like `delivery`, so it takes no participant id: reading is
        the caller's own state and no admin sets it for anybody else.

        Omitting `message_id` means "everything", which is what a client that
        has just opened the thread means. Naming a message is for the client
        that is looking at a specific point in the history.

        Answers with the conversation so the caller gets the recalculated
        unread_count back rather than having to assume it is now zero - it may
        not be, if something arrived between the read and this request.
        """
        conversation = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message_id = serializer.validated_data.get('message_id')
        if message_id is None:
            message = conversation.messages.order_by('-pk').first()
        else:
            message = conversation.messages.filter(pk=message_id).first()
            if message is None:
                raise NotFound('No such message in this conversation.')

        if message is not None:
            participant = Participant.objects.get(
                conversation=conversation, user=request.user
            )
            membership.set_last_read(participant, message)

        # Re-read through get_queryset so the response carries the annotation.
        conversation = self.get_queryset().get(pk=conversation.pk)
        return Response(self._as_conversation(conversation))

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

        # Everything before this member cleared their view is gone for them,
        # and must not come back through paging or the resume cursor.
        own = Participant.objects.filter(
            conversation=conversation, user=request.user
        ).first()
        cleared_before = own.cleared_before_message_id if own else None

        history = conversation.messages.select_related(
            *MESSAGE_SELECT_RELATED
        ).prefetch_related(*MESSAGE_PREFETCH_RELATED)

        if cleared_before is not None:
            history = history.filter(pk__gt=cleared_before)

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

    # -- one message -------------------------------------------------------
    #
    # Every route below hangs off the CONVERSATION, reached through
    # get_object(). That is deliberate: there is no top-level /messages/<id>/
    # route to bolt a membership check onto, and adding one would put the
    # access-control model in two places. Here the queryset has already proven
    # the caller is an active participant before the message is even looked up.

    def _message_in(self, conversation, message_id):
        """The message, or 404 - never a message from somewhere else.

        Scoped to the conversation the route resolved, so a member of group A
        cannot act on a message in group B by putting its id in the URL.
        """
        message = (
            conversation.messages.select_related(*MESSAGE_SELECT_RELATED)
            .prefetch_related(*MESSAGE_PREFETCH_RELATED)
            .filter(pk=message_id)
            .first()
        )
        if message is None:
            raise NotFound('No such message in this conversation.')
        return message

    def _message_response(self, message):
        """Re-read, broadcast, and answer with the same payload.

        Re-read because the instance just written still carries the prefetch
        cache from before the change - answering from it would show the caller
        the state they were trying to change.
        """
        fresh = self._message_in(message.conversation, message.pk)
        broadcast_message_updated(fresh)
        return Response(
            MessageSerializer(fresh, context=self.get_serializer_context()).data
        )

    @action(
        detail=True,
        methods=['patch', 'delete'],
        url_path=r'messages/(?P<message_id>[^/.]+)',
    )
    def message_detail(self, request, pk=None, message_id=None):
        """Edit or delete one message.

        Neither can recall a copy already delivered by SMS or email. Those went
        to a phone and an inbox the moment the message was sent; this changes
        what the APP shows and says nothing about what a handset already has.
        The UI has to be honest about that rather than implying a recall.
        """
        conversation = self.get_object()
        message = self._message_in(conversation, message_id)

        if request.method == 'DELETE':
            return self._delete_message(request, conversation, message)
        return self._edit_message(request, message)

    def _edit_message(self, request, message):
        """Change the words. The sender only, and only their own words.

        Not admins: an admin rewriting somebody else's message would put words
        in their mouth under their name. An admin who objects to a message can
        delete it, which is visible for what it is.
        """
        if message.sender_id != request.user.pk:
            raise PermissionDenied('You can only edit your own messages.')
        if message.is_deleted:
            raise ValidationError('That message was deleted.')
        if message.source != Message.Source.APP:
            # A text or an email is a copy of something that was sent
            # elsewhere. Editing the copy would make the thread disagree with
            # the message that actually arrived.
            raise ValidationError('Only messages sent from the app can be edited.')

        serializer = MessageEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        from chat.link_previews import first_link, schedule_link_preview

        previous_link = first_link(message.text)
        message.text = serializer.validated_data['text']
        message.edited_at = timezone.now()
        fields = ['text', 'edited_at', 'updated_at']
        if first_link(message.text) != previous_link and message.link_preview:
            # The old card describes a link that is gone; it goes at once, and
            # the new link's card follows as its own update.
            message.link_preview = None
            fields.append('link_preview')
        message.save(update_fields=fields)

        # The edited words name whoever the edited words name. Only when the
        # client says so: an older client that sends just `text` leaves the
        # existing mentions alone rather than silently clearing them.
        if 'mentions' in serializer.validated_data or (
            'mention_everyone' in serializer.validated_data
        ):
            from chat.mentions import set_mentions

            set_mentions(
                message,
                serializer.validated_data.get('mentions'),
                everyone=serializer.validated_data.get('mention_everyone', False),
            )

        if first_link(message.text) != previous_link:
            schedule_link_preview(message)
        return self._message_response(message)

    def _delete_message(self, request, conversation, message):
        """Take a message out of the thread, for everybody.

        The sender may delete their own; a group admin may delete anybody's,
        which is the moderation half of the feature.

        SOFT, always. The row stays so the replies quoting it keep a parent to
        point at and so `last_read_message` pointers do not dangle; the
        serializer is what withholds the text. A hard delete would take the
        replies' context with it and leave read receipts pointing at nothing.
        """
        is_sender = message.sender_id == request.user.pk
        if not is_sender and not conversation.is_admin(request.user):
            raise PermissionDenied(
                'You can only delete your own messages.'
            )

        if message.is_deleted:
            # Already gone. Answering with the message rather than an error
            # makes a retried delete harmless.
            return self._message_response(message)

        message.deleted_at = timezone.now()
        message.deleted_by = request.user
        message.save(update_fields=['deleted_at', 'deleted_by', 'updated_at'])

        # A pin is a place to keep something useful; a tombstone there is
        # clutter only an admin could clear. Deleting it clears the pin.
        if conversation.pinned_message_id == message.pk:
            conversation.pinned_message = None
            conversation.save(update_fields=['pinned_message'])
            broadcast_conversation_updated(conversation)

        return self._message_response(message)

    @action(
        detail=True,
        methods=['post', 'delete'],
        url_path=r'messages/(?P<message_id>[^/.]+)/reactions',
    )
    def message_reactions(self, request, pk=None, message_id=None):
        """Add or remove your own reaction.

        NEVER goes through create_message, and that is the point: a reaction is
        app-only. Routing it through the send path would fan it out to every
        phone in the group and bill a segment for a thumbs up.

        Both verbs are idempotent - POST twice adds one reaction, DELETE twice
        removes one - so a retry after a dropped response cannot flip the state
        back. A toggle endpoint could not promise that.
        """
        conversation = self.get_object()
        message = self._message_in(conversation, message_id)

        if message.is_deleted:
            raise ValidationError('That message was deleted.')

        if request.method == 'DELETE':
            # In the query string, not the body: a DELETE with a body is
            # inconsistently supported by clients and proxies, and this one
            # value fits in a parameter.
            serializer = MessageReactionSerializer(
                data={'emoji': request.query_params.get('emoji')}
            )
            serializer.is_valid(raise_exception=True)
            MessageReaction.objects.filter(
                message=message,
                user=request.user,
                emoji=serializer.validated_data['emoji'],
            ).delete()
            return self._message_response(message)

        serializer = MessageReactionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # get_or_create rather than create-and-catch: it takes a savepoint, so
        # the losing side of a race is recoverable. A bare IntegrityError here
        # marks the whole request transaction unusable and turns "you already
        # reacted" into a 500.
        MessageReaction.objects.get_or_create(
            message=message,
            user=request.user,
            emoji=serializer.validated_data['emoji'],
        )
        return self._message_response(message)

    @action(
        detail=True,
        methods=['post', 'delete'],
        url_path=r'messages/(?P<message_id>[^/.]+)/pin',
    )
    def message_pin(self, request, pk=None, message_id=None):
        """Hold one message above the thread, or let it go.

        ONE pin per conversation, stored as a column. Many pins would need a
        table and an order to show them in, and neither has been asked for.

        No new socket event: the change is broadcast as `conversation.updated`,
        which every client already handles.
        """
        conversation = self.get_object()
        message = self._message_in(conversation, message_id)

        if not conversation.may_pin(request.user):
            raise PermissionDenied('Only a group admin can pin a message.')

        if request.method == 'DELETE':
            # Scoped to THIS message, so two admins unpinning at once cannot
            # have the second one clear a pin the first already replaced.
            if conversation.pinned_message_id != message.pk:
                return Response(self._as_conversation(conversation))
            conversation.pinned_message = None
        else:
            if message.is_deleted:
                raise ValidationError('That message was deleted.')
            conversation.pinned_message = message

        conversation.save(update_fields=['pinned_message'])
        broadcast_conversation_updated(conversation)
        return Response(self._as_conversation(conversation))

    def _send_message(self, request, conversation):
        serializer = MessageCreateSerializer(
            data=request.data,
            # The conversation goes in the CONTEXT, not the payload, so
            # validate_reply_to checks the parent against the conversation the
            # route already resolved and a caller cannot widen it.
            context={**self.get_serializer_context(), 'conversation': conversation},
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
