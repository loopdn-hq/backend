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
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models


def attachment_upload_to(instance, filename):
    """Store attachments per conversation, under an unguessable name.

    The uploaded filename is attacker-controlled and frequently collides
    (`IMG_0001.jpg`), so it is kept in `original_name` for display and plays
    no part in the path.
    """
    suffix = Path(filename).suffix.lower()[:16]
    conversation_id = instance.message.conversation_id
    return f'chat/{conversation_id}/{uuid.uuid4().hex}{suffix}'


class Conversation(models.Model):
    class Kind(models.TextChoices):
        DIRECT = 'direct', 'Direct'
        GROUP = 'group', 'Group'

    kind = models.CharField(max_length=8, choices=Kind.choices)

    # Groups only. A direct conversation is named after the other person by
    # whoever is looking at it, so it cannot have one stored value.
    name = models.CharField(max_length=120, blank=True)

    # Direct conversations only: '<lower user id>:<higher user id>'.
    direct_key = models.CharField(max_length=64, unique=True, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversations_created',
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
    def start_group(cls, creator, name):
        """Create a group. The creator is its first admin."""
        conversation = cls.objects.create(
            kind=cls.Kind.GROUP, name=name, created_by=creator
        )
        conversation.add_participant(creator, role=Participant.Role.ADMIN)
        return conversation

    def add_participant(self, user, role=None):
        """Idempotent: re-adding somebody does not duplicate or demote them."""
        participant, _ = Participant.objects.get_or_create(
            conversation=self,
            user=user,
            defaults={'role': role or Participant.Role.MEMBER},
        )
        return participant

    def has_participant(self, user):
        return self.participants.filter(user=user).exists()

    def is_admin(self, user):
        return self.participants.filter(
            user=user, role=Participant.Role.ADMIN
        ).exists()


class Participant(models.Model):
    """One person's membership of one conversation."""

    class Role(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        MEMBER = 'member', 'Member'

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='participants',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversation_memberships',
    )
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # The membership check is an existence test; a duplicate row
            # would silently double every participant count.
            models.UniqueConstraint(
                fields=['conversation', 'user'], name='unique_conversation_participant'
            ),
        ]
        ordering = ['joined_at']

    def __str__(self):
        return f'{self.user.username} in {self.conversation}'

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN


class Message(models.Model):
    """One message. Text, attachments, or both.

    Emoji need no special handling: they are ordinary characters in a
    TextField, and both SQLite and Postgres store them as UTF-8. Links are
    likewise just text - the client renders them.
    """

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='messages_sent',
    )

    # Blank when the message is nothing but attachments. The serializer
    # rejects a message that has neither.
    text = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The one query that matters: a page of a conversation's history,
            # newest first.
            models.Index(fields=['conversation', '-created_at']),
        ]

    def __str__(self):
        preview = self.text[:40] or '(attachment)'
        return f'{self.sender.username}: {preview}'


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
