"""Conversations, membership and messages.

Two kinds of conversation share one table, because everything below them -
membership, messages, attachments, read state - is identical:

    DIRECT  exactly two people, created on demand, never duplicated
    GROUP   any number of people, created deliberately, has admins

`direct_key` is what stops two people ending up with two parallel
one-to-one threads: it holds the pair of user ids in a fixed order and is
unique, so a second attempt finds the first conversation instead of making
another. Groups leave it NULL, and both SQLite and Postgres allow any number
of NULLs under a unique constraint.
"""
import secrets
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q

from user.models import phone_number_validator


def group_image_upload_to(instance, filename):
    """Store a group's image beside that conversation's attachments.

    Same reasoning as `attachment_upload_to`: the uploaded filename is
    attacker-controlled and collides constantly, so only the suffix survives
    and the stored name is a uuid. Nothing displays this path, so unlike an
    attachment there is no `original_name` to keep alongside it.

    `pk` is None until the row has been inserted - a FileField is written in
    pre_save, which runs before the INSERT - which is why `start_group`
    attaches the image in a second save rather than passing it to create().
    The fallback only catches the admin's add form, which saves the file and
    the row together and would otherwise file it under a literal `None`.
    """
    suffix = Path(filename).suffix.lower()[:16] or '.jpg'
    return f'chat/{instance.pk or "unfiled"}/group/{uuid.uuid4().hex}{suffix}'


def attachment_upload_to(instance, filename):
    """Store attachments per conversation, under an unguessable name.

    The uploaded filename is attacker-controlled and frequently collides
    (`IMG_0001.jpg`), so it is kept in `original_name` for display and plays
    no part in the path.
    """
    suffix = Path(filename).suffix.lower()[:16]
    conversation_id = instance.message.conversation_id
    return f'chat/{conversation_id}/{uuid.uuid4().hex}{suffix}'


class PostingNotAllowed(Exception):
    """This conversation will not take a message from this member right now.

    Separate from the membership errors: the person IS a member in good
    standing, they simply may not speak here - a broadcast group where they are
    not an admin. loopd/exceptions.py maps it to a 403.
    """


class BlockedFromConversation(Exception):
    """Somebody blocked was offered a way back in, and it was refused.

    A plain exception rather than a DRF one, so the membership rules stay
    usable from a management command, a Celery task and the inbound-SMS path -
    none of which have a request. loopd/exceptions.py maps it to a 403.
    """


def local_part(email):
    """The name-ish half of an address, for showing in a group: ana.

    An email invitee has no account and no name yet, and the same reasoning as
    `mask_number` applies to the label: it goes out to everybody in the group.
    The local part identifies them to the person who invited them without
    handing the full address to every other member.
    """
    return (email or '').strip().split('@')[0][:80]


def mask_number(number):
    """A phone number safe to show other people: +63…1111.

    A member who joined by texting JOIN never gave a name, so their label falls
    back to their number - and that label goes out in the body of every message
    the group sends, to everybody in it. Printing it in full hands each member's
    number to every other member, which is the exact thing a per-group number is
    supposed to avoid. Enough digits to recognise yourself, not enough to dial.
    """
    number = (number or '').strip()
    if len(number) < 6:
        return number
    return f'{number[:3]}\u2026{number[-4:]}'


def generate_join_code(length=6):
    """A short code somebody can read off a screen and type into a text.

    Drawn from an alphabet with no 0/O, 1/I/L or 5/S, because this code's whole
    job is to survive being read aloud, written down and typed back in.
    """
    alphabet = 'ABCDEFGHJKMNPQRTUVWXYZ2346789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


class Conversation(models.Model):
    class Kind(models.TextChoices):
        DIRECT = 'direct', 'Direct'
        GROUP = 'group', 'Group'

    class Mode(models.TextChoices):
        # Everybody talks to everybody.
        CHAT = 'chat', 'Chat group'
        # Admins announce; members receive. Their replies do not go to the
        # group. Whether replies reach the admins privately or are refused
        # outright is a later choice - today they are simply not posted.
        BROADCAST = 'broadcast', 'Broadcast group'

    class JoinMode(models.TextChoices):
        # Anyone who has the number or the link is in, immediately.
        OPEN = 'open', 'Open'
        # They must supply the group's code as well.
        CODE = 'code', 'Code required'
        # They ask; an admin approves or declines.
        APPROVAL = 'approval', 'Admin approval'
        # Nobody can ask. Only an admin adds people.
        INVITE = 'invite', 'Invite only'

    class MemberAddPermission(models.TextChoices):
        ADMINS = 'admins', 'Admins only'
        MEMBERS = 'members', 'All members'

    class EditGroupPermission(models.TextChoices):
        ADMINS = 'admins', 'Admins only'
        MEMBERS = 'members', 'All members'

    kind = models.CharField(max_length=8, choices=Kind.choices)

    # Groups only. A direct conversation is named after the other person by
    # whoever is looking at it, so it cannot have one stored value.
    name = models.CharField(max_length=120, blank=True)

    # Groups only, and optional even for them: what the group is for, shown
    # under its name. Bounded at Profile.bio's 280 rather than left as a
    # TextField because this is the one piece of group text that a join
    # confirmation will want to quote back over SMS, where length is segments
    # and segments are billed.
    description = models.CharField(
        max_length=280, blank=True, default='', db_default=''
    )

    # Groups only. A direct conversation shows the other person's avatar, so
    # there is nothing for it to store here. `null=True` follows
    # Profile.avatar - "no image" stays one state rather than two - and is
    # also what lets the column carry no db_default; see migration 0003.
    image = models.ImageField(upload_to=group_image_upload_to, blank=True, null=True)

    # Direct conversations only: '<lower user id>:<higher user id>'.
    direct_key = models.CharField(max_length=64, unique=True, null=True, blank=True)

    ## GROUP SETTINGS ##

    # Groups only. Whether members may post, or only admins.
    mode = models.CharField(
        max_length=9, choices=Mode.choices, default=Mode.CHAT, db_default=Mode.CHAT
    )

    # Groups only. How somebody who is not yet a member gets in.
    #
    # Defaults to INVITE, which is the only mode that cannot surprise anybody:
    # every group that exists today was built by an admin adding people, and a
    # migration that silently made them joinable would be a privacy incident,
    # not a feature.
    join_mode = models.CharField(
        max_length=8,
        choices=JoinMode.choices,
        default=JoinMode.INVITE,
        db_default=JoinMode.INVITE,
    )
    # Unique across the product, not merely within a group: somebody texting
    # `JOIN FAMILY` has given us the code and nothing else, so the code alone
    # has to identify which group they mean.
    join_code = models.CharField(max_length=12, unique=True, null=True, blank=True)

    # Who may add/invite new members.
    add_member_permission = models.CharField(
        max_length=8,
        choices=MemberAddPermission.choices,
        default=MemberAddPermission.ADMINS,
        db_default=MemberAddPermission.ADMINS,
    )

    # Who may change the group's name, image and description.
    edit_group_permission = models.CharField(
        max_length=8,
        choices=EditGroupPermission.choices,
        default=EditGroupPermission.ADMINS,
        db_default=EditGroupPermission.ADMINS,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversations_created',
    )

    # -------------------------
    # SMS SETTINGS
    # -------------------------

    # OUTBOUND: a message posted here is texted to members.
    #
    # Named `sms_delivery_enabled` rather than `sms_enabled` because Profile and
    # Participant each already have an `sms_enabled`, at different scopes.
    sms_delivery_enabled = models.BooleanField(
        default=True,
        db_default=True,
    )

    # INBOUND: a text sent to this group's number is posted into the
    # conversation.
    sms_replies_enabled = models.BooleanField(
        default=True,
        db_default=True,
    )

    # -------------------------
    # MEDIA SETTINGS
    # -------------------------

    # Whether members may record/upload voice messages in this conversation.
    # Admins can disable this for groups where only text communication is wanted.
    voice_messages_enabled = models.BooleanField(
        default=True,
        db_default=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    # Touched on every new message so the conversation list can sort by it
    # without a join.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name or f'{self.get_kind_display()} #{self.pk}'

    @staticmethod
    def direct_key_for(user_a, user_b):
        """A stable key for a pair, whichever way round they are given."""
        low, high = sorted([int(user_a.pk), int(user_b.pk)])
        return f'{low}:{high}'

    @classmethod
    def start_direct(cls, user, other):
        """Return this pair's conversation, creating it only if needed."""
        if user.pk == other.pk:
            raise ValueError('A direct conversation needs two different people.')

        key = cls.direct_key_for(user, other)
        conversation = cls.objects.filter(direct_key=key).first()
        if conversation:
            # Re-activate both rows. `start_direct` is the only way back into a
            # one-to-one thread, and it is reached by `direct_key` rather than
            # by membership - so a row left at some non-active status would let
            # this endpoint keep serving the conversation, and its latest
            # message, to somebody the rest of the API has correctly locked out.
            # add_participant is idempotent and refuses anyone blocked.
            conversation.add_participant(user, role=Participant.Role.MEMBER)
            conversation.add_participant(other, role=Participant.Role.MEMBER)
            return conversation, False

        conversation = cls.objects.create(
            kind=cls.Kind.DIRECT, direct_key=key, created_by=user
        )
        # Nobody administers a direct conversation - there is nothing to
        # administer, and neither person outranks the other.
        conversation.add_participant(user, role=Participant.Role.MEMBER)
        conversation.add_participant(other, role=Participant.Role.MEMBER)
        return conversation, True

    @classmethod
    @transaction.atomic
    def start_group(cls, creator, name, description='', image=None):
        """Create a group. The creator is its first admin.

        Atomic, for the same reason `create_message` is: attaching the image
        writes to storage, which in staging and production is a network call
        to S3 that can time out. That write sits between the INSERT and
        `add_participant`, so without this the failure would leave a committed
        conversation with no participants - and since access control here IS
        the queryset (`participants__user=...`), such a row is invisible to
        its own creator and unreachable from every route in the app. One per
        retry, forever.
        """
        conversation = cls.objects.create(
            kind=cls.Kind.GROUP,
            name=name,
            description=description,
            created_by=creator,
            # Every group gets one from the start, so switching join_mode to
            # CODE later is a settings change rather than a migration. It is
            # inert until the mode calls for it.
            join_code=cls._unused_join_code(),
        )
        # Attached after the INSERT rather than in it: a FileField is written
        # during pre_save, before the row has a pk, so `group_image_upload_to`
        # would file every first image under `chat/unfiled/`.
        if image is not None:
            conversation.image = image
            conversation.save(update_fields=['image'])
        conversation.add_participant(creator, role=Participant.Role.ADMIN)
        return conversation

    def add_participant(self, user, role=None):
        """Idempotent: re-adding somebody does not duplicate or demote them.

        Idempotent across the two kinds of row, too. Somebody added as a bare
        phone number before they had an account, who has since registered and
        verified that number, is the *same person* - so their existing row is
        claimed rather than a second one created. Without that, they would sit
        in the group twice, be counted twice, and once SMS fan-out exists, be
        texted and billed twice for every message.

        Somebody who left or was removed is reactivated in place rather than
        duplicated - `UNIQUE(conversation, user)` forbids a second row, so
        without this the call would silently return their old, inactive row and
        the endpoint would report success while nothing happened.

        Somebody BLOCKED is not re-added at all. That status exists precisely
        to survive an admin who forgets, or a JOIN text from a number that was
        shown the door.
        """
        claimed = self._claim_phone_participant(user)
        if claimed is not None:
            return claimed

        participant, created = Participant.objects.get_or_create(
            conversation=self,
            user=user,
            defaults={
                'role': role or Participant.Role.MEMBER,
                'status': Participant.Status.ACTIVE,
            },
        )
        if created:
            return participant

        if participant.status == Participant.Status.BLOCKED:
            raise BlockedFromConversation(
                f'{participant.display_label} is blocked from this conversation.'
            )

        if participant.status != Participant.Status.ACTIVE:
            participant.status = Participant.Status.ACTIVE
            participant.save(update_fields=['status'])

        return participant

    def _claim_phone_participant(self, user):
        """Fold this conversation's phone-only row for `user` into their account.

        Returns the surviving Participant, or None when there was no such row.

        Only a *verified* number may be claimed. An unverified one is just a
        string somebody typed into their own profile, and honouring it would
        hand them the membership - and the history - of whoever really owns
        that number.

        A blocked phone row is never claimed either. Registering an account
        against a number that was blocked must not be a way back in.
        """
        profile = getattr(user, 'profile', None)
        if profile is None or not (profile.phone_no_verified and profile.phone_number):
            return None

        # Participant.objects, not self.participants: the viewset prefetches
        # that relation filtered to ACTIVE, and a related manager filters the
        # prefetch cache - so a BLOCKED row would be invisible here and the
        # block would silently not apply.
        phone_row = Participant.objects.filter(
            conversation=self, user__isnull=True, phone_number=profile.phone_number
        ).first()
        if phone_row is None:
            return None

        if phone_row.status == Participant.Status.BLOCKED:
            raise BlockedFromConversation(
                'That number is blocked from this conversation.'
            )

        existing = Participant.objects.filter(conversation=self, user=user).first()
        if existing is None:
            phone_row.user = user
            phone_row.status = Participant.Status.ACTIVE
            phone_row.save(update_fields=['user', 'status'])
            return phone_row

        # Already here as an account *and* as a number: one human, two rows.
        # Keep the account row and move the history across before dropping the
        # duplicate - `sender_participant` is SET_NULL, so deleting first would
        # silently orphan every message they texted in.
        phone_row.messages_sent.update(sender_participant=existing)
        phone_row.delete()

        if existing.status == Participant.Status.BLOCKED:
            raise BlockedFromConversation(
                f'{existing.display_label} is blocked from this conversation.'
            )
        if existing.status != Participant.Status.ACTIVE:
            existing.status = Participant.Status.ACTIVE
            existing.save(update_fields=['status'])
        return existing

    def add_phone_participant(self, phone_number, display_name, added_by=None):
        """Add somebody who has no account, reachable only at this number.

        `sms_enabled` is True and consent starts PENDING: SMS is the only way
        this member can receive anything, so switching it off would leave them
        in a group that never reaches them - but nothing is actually sent until
        they answer the invitation, which is what PENDING gates.
        """
        participant, _ = Participant.objects.get_or_create(
            conversation=self,
            phone_number=phone_number,
            defaults={
                'display_name': display_name,
                'added_by': added_by,
                'role': Participant.Role.MEMBER,
                'sms_enabled': True,
                'sms_consent': Participant.Consent.PENDING,
            },
        )
        return participant

    @classmethod
    def _unused_join_code(cls, attempts=12):
        """A join code no other conversation is holding.

        `join_code` is unique, so a collision would surface as an IntegrityError
        from group creation - a 409 on an endpoint whose caller did nothing
        wrong. At 29**6 the odds are remote, but "remote" over a unique column
        is a bug that appears once and cannot be reproduced.
        """
        for _ in range(attempts):
            code = generate_join_code()
            if not cls.objects.filter(join_code=code).exists():
                return code
        # Twelve collisions in a row means the space is genuinely crowded, not
        # that we were unlucky. Longer codes, rather than a silent duplicate.
        return generate_join_code(length=10)

    def regenerate_join_code(self):
        """Invalidate the old code. Anyone holding it can no longer join."""
        self.join_code = self._unused_join_code()
        self.save(update_fields=['join_code'])
        return self.join_code

    @property
    def active_participants(self):
        """The people actually in this conversation right now.

        Everything that asks "who is in this?" must go through here or filter
        the same way. A pending, departed, removed or blocked row is still a
        row, and `Conversation.participants` still returns it.
        """
        # Participant.objects rather than the relation: a prefetched
        # conversation caches its participants, and a related manager filters
        # that cache. It happens to give the right answer today because the
        # prefetch is itself ACTIVE-only, which is exactly the kind of accident
        # that stops being true the moment somebody changes the prefetch.
        return Participant.objects.filter(
            conversation=self, status=Participant.Status.ACTIVE
        )

    def allows_posting_by(self, participant):
        """May this member put a message into this conversation?

        Broadcast is the only thing that says no today. Membership itself is
        checked long before here - by the queryset that found the conversation
        at all.
        """
        if self.mode != self.Mode.BROADCAST:
            return True
        return participant is not None and participant.role == Participant.Role.ADMIN

    def has_participant(self, user):
        return self.active_participants.filter(user=user).exists()

    def has_phone_participant(self, phone_number):
        return self.active_participants.filter(phone_number=phone_number).exists()

    def may_add_members(self, user):
        """Whether this person may add or invite people."""
        if self.add_member_permission == self.MemberAddPermission.MEMBERS:
            return self.has_participant(user)
        return self.is_admin(user)

    def may_edit_group(self, user):
        """Whether this person may change the name, image or description.

        Never covers the policy settings - see ADMIN_ONLY_SETTINGS. A member who
        could grant themselves permissions would make this setting pointless.
        """
        if self.edit_group_permission == self.EditGroupPermission.MEMBERS:
            return self.has_participant(user)
        return self.is_admin(user)

    def is_admin(self, user):
        return self.active_participants.filter(
            user=user, role=Participant.Role.ADMIN
        ).exists()


class Participant(models.Model):
    """One person's membership of one conversation.

    A participant is either an account or a phone number, never neither:

        user set, phone_number null    an ordinary LoopD member
        user null, phone_number set    somebody reachable only by SMS, who has
                                       no account and may never make one

    That second kind is what lets a group include people who never install
    anything. They are full members - their messages arrive over SMS and appear
    in the thread like everybody else's - so membership, not an account, is what
    the rest of the app should test for.

    `display_name` is required for a phone-only row, because a group showing
    `+639171111111` instead of "Mom" is both unhelpful and a privacy leak: the
    per-group number design otherwise keeps members' numbers away from each
    other. When that person later registers and verifies the number, `user` is
    filled in and their own display name takes over.
    """

    class Role(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        MEMBER = 'member', 'Member'

    class Status(models.TextChoices):
        # A real member: reads the history, posts, receives.
        ACTIVE = 'active', 'Active'
        # Asked to join a group whose join_mode is APPROVAL, and is waiting.
        # Not a member yet - no history, no delivery, no posting.
        PENDING = 'pending', 'Pending approval'
        # Walked out themselves. May come back.
        LEFT = 'left', 'Left'
        # An admin took them out. May be re-added.
        REMOVED = 'removed', 'Removed'
        # An admin took them out and they may NOT come back, by any route -
        # not by texting JOIN, not by an admin adding them again by mistake.
        BLOCKED = 'blocked', 'Blocked'

    class Consent(models.TextChoices):
        # An account holder agreed to SMS in their own settings, so there is
        # nothing to collect here.
        NOT_REQUIRED = 'not_required', 'Not required'
        # A phone-only member has been texted an invitation and has not
        # answered. No group traffic reaches them in this state.
        PENDING = 'pending', 'Pending'
        GRANTED = 'granted', 'Granted'
        # They replied STOP. Never text this row again, whatever the settings
        # elsewhere say.
        STOPPED = 'stopped', 'Stopped'

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='participants',
    )
    # Null for somebody who exists only as a phone number. Everything that
    # filters on `participants__user=<someone>` therefore skips these rows
    # automatically, which is what keeps the access-control queryset correct
    # without a single change.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversation_memberships',
        null=True,
        blank=True,
    )
    phone_number = models.CharField(
        max_length=16,
        null=True,
        blank=True,
        validators=[phone_number_validator],
        help_text='Set only for members who have no account.',
    )
    # What the group sees. Required for a phone-only row; ignored for a member
    # who has an account, because their own profile is more current.
    # Somebody invited by email who has no account yet. Nullable and unique
    # per conversation for the same reason `phone_number` is: it identifies a
    # person we cannot point at any other way, and two rows for one address
    # would double every count and every send.
    #
    # It is CLEARED when the row is claimed by a verified account, so a row
    # never carries both an owner and the address that proved them - see
    # `_claim_email_participant`.
    email = models.EmailField(max_length=254, null=True, blank=True)
    display_name = models.CharField(
        max_length=80, blank=True, default='', db_default=''
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='participants_added',
        null=True,
        blank=True,
    )

    role = models.CharField(max_length=8, choices=Role.choices, default=Role.MEMBER)

    # A soft delete over the existing (conversation, user) unique row, so that
    # leaving and coming back reuses one row rather than colliding with it -
    # and so that BLOCKED survives being re-added.
    #
    # Every membership test in the project filters on this. That is not
    # optional: `Conversation.participants` is what the access-control queryset
    # joins through, so a row left at its old meaning would keep a removed
    # member reading the history and receiving the fan-out.
    status = models.CharField(
        max_length=8,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_default=Status.ACTIVE,
    )
    # The member silenced this group themselves - by texting MUTE, or from the
    # UI. Distinct from `sms_enabled`, which is whether SMS is a channel for
    # them at all: muting is the member's own act and covers every channel,
    # where sms_enabled is a delivery preference an admin screen also touches.
    muted = models.BooleanField(default=False, db_default=False)

    # Whether this membership receives a text when somebody posts. ON by
    # default, for everybody.
    #
    # That is the product: a member verifies their phone number when they create
    # their account, and that verified number is what the group texts. Somebody
    # with the app installed still gets the text - the app and SMS are two ways
    # into one conversation, not alternatives - which is why this does not
    # depend on whether they have an account.
    #
    # It costs nothing for a member with no verified number: the fan-out refuses
    # to text anybody it cannot address, so this flag is about intent and the
    # verified number is what makes it happen.
    sms_enabled = models.BooleanField(default=True, db_default=True)
    sms_consent = models.CharField(
        max_length=12,
        choices=Consent.choices,
        default=Consent.NOT_REQUIRED,
        db_default=Consent.NOT_REQUIRED,
    )
    consent_sent_at = models.DateTimeField(null=True, blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    # Verbatim text of the invitation we sent. Kept because "what exactly were
    # they agreeing to" is the question a consent complaint actually turns on,
    # and the template will have changed by the time anybody asks.
    consent_text = models.TextField(blank=True, default='', db_default='')

    # How far this member has read, as the last message they have seen.
    #
    # A message id rather than a timestamp, for the reason the history cursor
    # gives: created_at is app-server wall clock, so two tasks can stamp
    # messages out of order and it has no tiebreak. Ids come from one sequence
    # and are a sound total order, so "newer than this" is exact.
    #
    # NULL means nothing has been read - every message in the group counts as
    # unread. That is also the permanent state of a phone-only member: SMS can
    # tell you a handset accepted a text and never that a person read it, so
    # there is nothing here to move and no client asking for their badge.
    #
    # related_name='+' on purpose. The reverse of this - "everybody who has
    # read up to this message" - is the expensive query behind read receipts,
    # and it should not be reachable by accident.
    last_read_message = models.ForeignKey(
        'chat.Message',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    # Everything up to here is gone FOR THIS MEMBER. They did not leave and
    # nobody else's copy changed - they simply cleared their own view of the
    # thread, and the history endpoint starts after this message for them.
    #
    # A message id rather than a timestamp, for the reason the resume cursor
    # and the read pointer both give: created_at is app-server wall clock and
    # has no tiebreak, where ids are a sound total order.
    #
    # The conversation disappears from their list while nothing has arrived
    # since - there is nothing left to show - and comes back by itself when
    # somebody posts, holding only what came after. That falls out of this
    # one field rather than needing a second 'hidden' flag to keep in step.
    cleared_before_message = models.ForeignKey(
        'chat.Message',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    # Tidied out of this member's own list. Theirs alone - nobody else can
    # tell, and it neither leaves the group nor stops anything being
    # delivered. That last part matters here more than in a pure chat app:
    # if archiving quietly stopped the texts, somebody could tidy their list
    # and then miss the messages still arriving on their phone. Muting is
    # what stops delivery; this only decides which list it appears in.
    archived_at = models.DateTimeField(null=True, blank=True)

    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # The membership check is an existence test; a duplicate row
            # would silently double every participant count. NULL user_ids do
            # not collide under this, which is what lets any number of
            # phone-only rows sit beside it.
            models.UniqueConstraint(
                fields=['conversation', 'user'], name='unique_conversation_participant'
            ),
            # The same number must not be in one group twice - otherwise an
            # inbound SMS resolves ambiguously and the sender is billed twice
            # for every fan-out.
            models.UniqueConstraint(
                fields=['conversation', 'phone_number'],
                condition=Q(phone_number__isnull=False),
                name='unique_conversation_phone_participant',
            ),
            # An address must not sit in one group twice, for the same reason
            # a number must not: the claim on verification would resolve
            # ambiguously and the invitation would be sent twice.
            models.UniqueConstraint(
                fields=['conversation', 'email'],
                condition=Q(email__isnull=False),
                name='unique_conversation_email_participant',
            ),
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False)
                    | Q(phone_number__isnull=False)
                    | Q(email__isnull=False)
                ),
                name='participant_identifies_someone',
            ),
            # Enforced here rather than only in the serializer: a nameless
            # phone-only row would render as a bare number to everyone in the
            # group, and the shell and the admin can both create one.
            models.CheckConstraint(
                condition=Q(user__isnull=False) | ~Q(display_name=''),
                name='phone_participant_has_a_name',
            ),
        ]
        ordering = ['joined_at']

    def __str__(self):
        return f'{self.display_label} in {self.conversation}'

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN

    @property
    def is_phone_only(self):
        """No account, reachable only at a number.

        Narrowed when email invitations arrived: `user_id is None` alone would
        now call an email invitee phone-only, and the label for one of those
        would fall through to masking a number that is not there.
        """
        return self.user_id is None and self.phone_number is not None

    @property
    def is_email_only(self):
        """No account yet, invited at an address and waiting to claim it."""
        return self.user_id is None and self.email is not None

    @property
    def display_label(self):
        """The name to show for this member, never empty.

        An account's own profile wins: it is the name they chose and keep
        current, where `display_name` is whatever the person who added them
        typed once.
        """
        if self.user_id is not None:
            return self.user.profile.display_label
        if self.display_name:
            return self.display_name
        if self.email:
            return local_part(self.email) or 'Unknown'
        return mask_number(self.phone_number) or 'Unknown'


class Message(models.Model):
    """One message. Text, attachments, or both.

    Emoji need no special handling: they are ordinary characters in a
    TextField, and both SQLite and Postgres store them as UTF-8. Links are
    likewise just text - the client renders them.

    There is one row per message however it arrived and however it is delivered.
    A message texted in from a handset and a message typed in the app are the
    same kind of thing, distinguished only by `source`; SMS deliveries are
    logged separately, one row per recipient, in the sms app. Anything that
    stored a second copy per channel would have to keep them in step forever.
    """

    class Source(models.TextChoices):
        APP = 'app', 'App'
        SMS = 'sms', 'SMS'
        SYSTEM = 'system', 'System'

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    # Null when the sender has no account - somebody texting in from a phone
    # number that is a member of the group. `sender_participant` is the field
    # that is always populated; this one stays because most senders do have an
    # account and every existing query, serializer and client reads it.
    #
    # on_delete stays CASCADE, deliberately: deleting a User has always taken
    # their messages with it, and turning that into SET_NULL here would quietly
    # change what account deletion means for everyone else's history. Making
    # the column nullable is what this feature needs; changing deletion
    # semantics is not.
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='messages_sent',
        null=True,
        blank=True,
    )
    # Who, in this conversation, said it - the one attribution that works for
    # accounts and phone-only members alike. SET_NULL because a message must
    # outlive a membership change; `create_message` keeps the invariant
    # `sender == sender_participant.user`.
    sender_participant = models.ForeignKey(
        Participant,
        on_delete=models.SET_NULL,
        related_name='messages_sent',
        null=True,
        blank=True,
    )
    source = models.CharField(
        max_length=8,
        choices=Source.choices,
        default=Source.APP,
        db_default=Source.APP,
    )

    # Blank when the message is nothing but attachments. The serializer
    # rejects a message that has neither.
    text = models.TextField(blank=True)

    # The sender's own id for this message, so a retry cannot post it twice.
    #
    # A websocket send is not acknowledged, so a client whose socket drops mid
    # send cannot know whether the message landed. Retrying used to create a
    # SECOND row with a different id - which client-side dedupe cannot collapse,
    # and which schedules a second billed SMS fan-out to every member's phone.
    #
    # Empty for anything the client did not key: inbound SMS, and older
    # clients. The constraint below is partial for exactly that reason.
    client_id = models.CharField(
        max_length=64, blank=True, default='', db_default=''
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The one query that matters: a page of a conversation's history,
            # newest first.
            models.Index(fields=['conversation', '-created_at']),
        ]
        constraints = [
            # The database is what actually makes a retry safe. Two sends
            # racing on the same key would both pass a SELECT-then-INSERT
            # check; only a unique index refuses the second, and
            # `create_message` turns that refusal back into the first message.
            models.UniqueConstraint(
                fields=['conversation', 'client_id'],
                condition=~models.Q(client_id=''),
                name='unique_conversation_client_message',
            ),
        ]

    def __str__(self):
        preview = self.text[:40] or '(attachment)'
        return f'{self.sender_label}: {preview}'

    @property
    def sender_label(self):
        """Who to credit, whether or not they have an account."""
        if self.sender_id is not None:
            return self.sender.username
        if self.sender_participant_id is not None:
            return self.sender_participant.display_label
        return 'Unknown'


class Attachment(models.Model):
    """A photo or file hanging off a message.

    Separate from Message so one message can carry several photos, which is
    what people actually do.
    """

    class Kind(models.TextChoices):
        IMAGE = 'image', 'Image'
        FILE = 'file', 'File'

    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name='attachments',
    )
    file = models.FileField(upload_to=attachment_upload_to)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.FILE)

    # What the sender called it. Shown in the UI; never used as a path.
    original_name = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.PositiveBigIntegerField(default=0)

    # Images only, so a client can reserve the right space before loading.
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.original_name or f'{self.get_kind_display()} #{self.pk}'


class Notification(models.Model):
    """One thing that happened to one person.

    Deliberately NOT one row per message. A two-hundred-member group posting a
    hundred times a day would write twenty thousand rows to say what
    `Participant.last_read_message` and `unread_count` already say, and then
    there would be two answers to "is there anything new here" to keep in
    agreement. Messages are unread counts; this table is for the events that
    currently exist only as a socket frame and vanish if nobody was looking -
    being added to a group, asked to join one, promoted, or told that a member
    has stopped receiving your texts.

    `conversation` and `message` are real foreign keys rather than one untyped
    id column, so the database can enforce what a row points at and clean up
    after a deleted group on its own.
    """

    class Type(models.TextChoices):
        # Membership - things that happened to you.
        MEMBER_ADDED = 'member.added', 'Added to a group'
        MEMBER_REMOVED = 'member.removed', 'Removed from a group'
        ROLE_GRANTED = 'role.granted', 'Made an admin'
        ROLE_REVOKED = 'role.revoked', 'No longer an admin'

        # Getting in.
        JOIN_REQUESTED = 'join.requested', 'Someone asked to join'
        JOIN_APPROVED = 'join.approved', 'Your request was approved'
        JOIN_DECLINED = 'join.declined', 'Your request was declined'

        # The group changed under you.
        GROUP_MODE_CHANGED = 'group.mode_changed', 'Switched to broadcast'

        # SMS - the failures nobody can currently see.
        SMS_OPTED_OUT = 'sms.opted_out', 'A member texted STOP'
        SMS_OPTED_IN = 'sms.opted_in', 'A member accepted by text'
        SMS_SEND_FAILED = 'sms.send_failed', 'A message did not reach everyone'
        SMS_INVITE_PENDING = 'sms.invite_pending', 'An invitation is unanswered'

        GENERAL = 'general', 'General'

    # The filter chips a client offers - Groups, Invites, SMS, Account -
    # are the dot prefix of the type, so a new type joins the right chip
    # without anybody maintaining a second mapping that can disagree.
    CATEGORY_BY_PREFIX = {
        'member': 'groups',
        'role': 'groups',
        'group': 'groups',
        'join': 'invites',
        'invite': 'invites',
        'sms': 'sms',
        'account': 'account',
        'security': 'account',
    }

    @property
    def category(self):
        prefix = self.type.split('.', 1)[0]
        return self.CATEGORY_BY_PREFIX.get(prefix, 'general')

    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='notifications',
        on_delete=models.CASCADE,
    )
    # Who caused it, where a person did. NULL for anything the system decided
    # on its own, and SET_NULL rather than CASCADE because deleting the actor
    # must not delete the RECIPIENT's record of what happened to them.
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='sent_notifications',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    type = models.CharField(
        max_length=60, choices=Type.choices, default=Type.GENERAL
    )

    conversation = models.ForeignKey(
        'chat.Conversation',
        related_name='notifications',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    message = models.ForeignKey(
        'chat.Message',
        related_name='+',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    # Anything the row needs that is not worth a column - the display name of a
    # phone-only member who has no account to point at, for instance.
    data = models.JSONField(null=True, blank=True)

    # When, not whether: the same information plus the time, and it matches
    # consent_at and last_seen_at rather than inventing a second convention.
    read_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The only query there is: my activity, newest first.
            models.Index(fields=['to_user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.get_type_display()} -> {self.to_user_id}'
