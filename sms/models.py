"""The SMS side of a conversation: its number, and what was sent over it.

Three tables, each with one job:

    GroupNumber      which phone number belongs to which group. This is what
                     turns an incoming text into a conversation.
    SmsDelivery      one row per message per recipient. The billing record.
    SmsWebhookEvent  which provider events we have already handled, so a retry
                     does not post the same text twice.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Anything about a specific provider beyond an opaque id. Per the product plan
(§15, §20) the provider, the number strategy and the carrier rules belong to the
telecom specialist, and core code talks to them through a normalised interface.
So there is no `telnyx_` prefix on any column: `provider` names who it was and
`provider_message_id` is their handle for it, whoever they turn out to be.
"""
from django.conf import settings
from django.core.cache import cache
from django.db import models

from chat.models import Conversation, Message, Participant
from user.models import phone_number_validator


class GroupNumber(models.Model):
    """The phone number people text to reach one group.

    Separate from Conversation rather than a column on it, because a number has
    a life of its own: it is leased before a group needs it, released when the
    group is deleted, and must NOT be handed to a different group immediately
    afterwards - somebody still has it saved in their phone as "Family", and
    their next text would land in a stranger's conversation.

    That is what `status` is for. RELEASED is not AVAILABLE.
    """

    class Status(models.TextChoices):
        # Leased from the provider, not yet given to a group.
        AVAILABLE = 'available', 'Available'
        ACTIVE = 'active', 'Active'
        # Detached from its group and serving a quarantine period. Never
        # reassigned from this state - only a human moves it on.
        RELEASED = 'released', 'Released'

    number = models.CharField(
        max_length=16, unique=True, validators=[phone_number_validator]
    )
    conversation = models.OneToOneField(
        Conversation,
        on_delete=models.SET_NULL,
        related_name='sms_number',
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=9,
        choices=Status.choices,
        default=Status.AVAILABLE,
        db_default=Status.AVAILABLE,
    )
    provider = models.CharField(max_length=32, blank=True, default='', db_default='')
    # The provider's own handle for this number, needed to reconfigure or
    # release it. Opaque to us.
    provider_number_id = models.CharField(
        max_length=64, blank=True, default='', db_default=''
    )
    country = models.CharField(max_length=2, blank=True, default='', db_default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['number']

    def __str__(self):
        return f'{self.number} -> {self.conversation or "(unassigned)"}'

    @classmethod
    def for_number_of(cls, conversation):
        """The number this group sends FROM, or None if it has not got one."""
        return cls.objects.filter(
            conversation=conversation, status=cls.Status.ACTIVE
        ).first()

    @classmethod
    def for_number(cls, number):
        """The group a text to this number belongs to, or None.

        The entire routing table. ACTIVE only: a released number must not go on
        delivering into the group it used to serve.
        """
        return (
            cls.objects.select_related('conversation')
            .filter(number=number, status=cls.Status.ACTIVE)
            .exclude(conversation__isnull=True)
            .first()
        )


class SmsWebhookEvent(models.Model):
    """One provider event we have already dealt with.

    Providers retry: any response they do not read as success arrives again,
    sometimes minutes later. Without this table a retry posts the same text into
    the group a second time, and re-runs the whole fan-out - every duplicate a
    paid message to a real person.

    Keyed on the EVENT id, never the message id. One outbound message emits
    several events (sent, then delivered) that share a message id and differ by
    event id; deduplicating on the message id would silently drop every status
    update after the first.
    """

    provider_event_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=48, blank=True, default='', db_default='')
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [models.Index(fields=['received_at'])]

    def __str__(self):
        return f'{self.event_type or "event"} {self.provider_event_id}'


class SmsDelivery(models.Model):
    """One message, to one recipient, over SMS. The billing record.

    §17 asks for "delivery/usage event logging needed for future billing", and
    this is it: who caused a message, which group it belonged to, how many
    billed segments it became, and whether it arrived.

    The counterpart's number is stored as a hash plus its last four digits
    rather than in full, mirroring otp/services.py. This table would otherwise
    become a permanent, growing record of exactly who texts which private group,
    sitting in every backup - and last-four is enough to support a person who
    writes in asking why they got a message.
    """

    class Direction(models.TextChoices):
        INBOUND = 'inbound', 'Inbound'
        OUTBOUND = 'outbound', 'Outbound'

    class Status(models.TextChoices):
        RECEIVED = 'received', 'Received'
        QUEUED = 'queued', 'Queued'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        DELIVERED = 'delivered', 'Delivered'
        FAILED = 'failed', 'Failed'
        # Deliberately not sent: muted, opted out, over budget, no number.
        # Recorded because "why did I not get that" is a support question.
        SKIPPED = 'skipped', 'Skipped'

    direction = models.CharField(max_length=8, choices=Direction.choices)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='sms_deliveries'
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        related_name='sms_deliveries',
        null=True,
        blank=True,
    )
    participant = models.ForeignKey(
        Participant,
        on_delete=models.SET_NULL,
        related_name='sms_deliveries',
        null=True,
        blank=True,
    )
    # Who to bill this to, when there is a wallet to bill. Null for somebody
    # with no account.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='sms_deliveries',
        null=True,
        blank=True,
    )

    counterpart_last4 = models.CharField(
        max_length=4, blank=True, default='', db_default=''
    )
    counterpart_hash = models.CharField(
        max_length=64, blank=True, default='', db_default=''
    )

    status = models.CharField(
        max_length=9,
        choices=Status.choices,
        default=Status.QUEUED,
        db_default=Status.QUEUED,
    )
    reason = models.CharField(max_length=120, blank=True, default='', db_default='')

    # How many times we have tried to send this. Bounded, because a number
    # that is disconnected fails identically every time and retrying it forever
    # is a standing charge for nothing.
    attempts = models.PositiveSmallIntegerField(default=0, db_default=0)

    segments = models.PositiveSmallIntegerField(default=0, db_default=0)
    encoding = models.CharField(max_length=8, blank=True, default='', db_default='')

    provider = models.CharField(max_length=32, blank=True, default='', db_default='')
    # Indexed, NOT unique: one message emits several status events that all
    # carry this same id.
    provider_message_id = models.CharField(
        max_length=128, blank=True, default='', db_default='', db_index=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # One outbound row per message per recipient. This is what makes a
            # retried fan-out safe: the second attempt collides instead of
            # texting somebody their neighbour's dinner plans twice.
            models.UniqueConstraint(
                fields=['message', 'participant'],
                condition=models.Q(direction='outbound'),
                name='one_outbound_sms_per_recipient',
            ),
        ]
        indexes = [
            models.Index(fields=['conversation', '-created_at']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f'{self.direction} {self.status} ...{self.counterpart_last4}'


class SmsSettings(models.Model):
    """The one row that can stop every outbound message, immediately.

    A database row and not an environment variable, deliberately. Flipping an
    ECS variable means registering a task definition and waiting out a rolling
    deploy - the workflow polls for up to 900 seconds. When you need this switch
    you have discovered a loop that is texting real people and billing you for
    it, and 15 minutes is not a response time.

    Cached, because it is read once per fan-out and the answer changes about
    once a year.
    """

    CACHE_KEY = 'sms:settings:enabled'

    singleton = models.BooleanField(
        primary_key=True,
        default=True,
        db_default=True,
        help_text='Always True. Enforced as the primary key so there is one row.',
    )
    sending_enabled = models.BooleanField(
        default=True,
        db_default=True,
        help_text='Turn off to stop all outbound SMS at once. Takes effect in seconds.',
    )
    note = models.CharField(
        max_length=200,
        blank=True,
        default='',
        db_default='',
        help_text='Why it was turned off, for whoever finds it later.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'SMS settings'
        verbose_name_plural = 'SMS settings'

    def __str__(self):
        return 'sending on' if self.sending_enabled else 'sending OFF'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    @classmethod
    def sending_is_on(cls):
        """Whether outbound SMS is allowed right now.

        Fails OPEN when the row does not exist, because a fresh install has no
        row and should behave like the settings flag says. It fails open on a
        cache miss too - the database is the authority, the cache is only there
        to keep this off the hot path.
        """
        cached = cache.get(cls.CACHE_KEY)
        if cached is not None:
            return cached

        row = cls.objects.first()
        enabled = True if row is None else row.sending_enabled
        cache.set(cls.CACHE_KEY, enabled, 60)
        return enabled
