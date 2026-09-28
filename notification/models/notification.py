"""One row of somebody's activity feed."""
from django.conf import settings
from django.db import models


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

    This model used to live in chat. It moved apps (chat/0022,
    notification/0001) without its table moving: see Meta.
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

        # Email, the same question asked of the other channel.
        EMAIL_SEND_FAILED = (
            'email.send_failed', 'A message did not reach everyone by email'
        )
        # The two ends of the consent handshake with somebody who has no
        # account. The sender asked a question and is waiting on it, so both
        # answers are news - and without these the answer would only be visible
        # to somebody who happened to reopen the thread.
        EMAIL_OPTED_IN = 'email.opted_in', 'Someone accepted by email'
        EMAIL_DECLINED = 'email.declined', 'Someone declined email messages'

        GENERAL = 'general', 'General'

    # The filter chips a client offers - Groups, Invites, Delivery, Account -
    # are the dot prefix of the type, so a new type joins the right chip
    # without anybody maintaining a second mapping that can disagree.
    #
    # `sms` and `email` share the DELIVERY chip (2026-09-16, Yul's call): both
    # answer the same question - did my message get out - and a chip per
    # channel would grow with every channel we add.
    CATEGORY_BY_PREFIX = {
        'member': 'groups',
        'role': 'groups',
        'group': 'groups',
        'join': 'invites',
        'invite': 'invites',
        'sms': 'delivery',
        'email': 'delivery',
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
        # The table keeps the name chat/0011 created it with. The model moved
        # apps; the rows did not, and renaming the table would be a real
        # migration against every row for no gain.
        db_table = 'chat_notification'
        ordering = ['-created_at']
        indexes = [
            # The only query there is: my activity, newest first. Named as
            # chat/0011 created it, for the same reason.
            models.Index(
                fields=['to_user', '-created_at'],
                name='chat_notifi_to_user_be9887_idx',
            ),
        ]

    def __str__(self):
        return f'{self.get_type_display()} -> {self.to_user_id}'
