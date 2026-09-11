"""What the email channel has to remember.

Two tables, both of them mirrors of the ones `sms` already has, and for the
same reasons rather than for symmetry's sake:

  EmailDelivery      answers "why did I not get that" - the single most common
                     support question about any delivery channel, and one the
                     application cannot answer at all unless it wrote the
                     reason down at the time.
  EmailWebhookEvent  makes an inbound reply idempotent. Providers retry, and a
                     retry that is not recognised posts the same reply into the
                     group a second time.

Email is far cheaper than SMS, so this is not a billing record the way
`SmsDelivery` is. It is a diagnosis record, which is why it keeps the reason
and the outcome and deliberately does not keep the message body.
"""
from django.conf import settings
from django.db import models

from chat.models import Conversation, Message, Participant


class EmailWebhookEvent(models.Model):
    """One inbound provider event we have already dealt with.

    Keyed on the EVENT id and never on the message id, exactly as
    `sms.SmsWebhookEvent` is: a single email can produce several events that
    share a message id, and deduplicating on that would drop all but the first.
    """

    provider_event_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=48, blank=True, default='', db_default='')
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [models.Index(fields=['received_at'])]

    def __str__(self):
        return f'{self.event_type or "event"} {self.provider_event_id}'


class EmailDelivery(models.Model):
    """One message, to one recipient, over email.

    The recipient's address is stored as a hash plus its domain, never in full.
    A table holding every address that receives every private group's messages
    is a mailing list of exactly who talks to whom, sitting in every backup;
    the domain is what is actually useful when a whole provider starts
    bouncing, and the hash is enough to answer one person asking about one
    message.
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
        # Deliberately not sent: muted, declined, no verified address, the
        # group is too large. Recorded rather than dropped - see the docstring.
        SKIPPED = 'skipped', 'Skipped'
        # What the provider told us afterwards, via mail/receipts.py. Neither
        # is FAILED, and that is not pedantry: the retry sweep reads FAILED
        # rows, and re-sending to an address that bounced or to a person who
        # marked us as spam is the one thing it must never do.
        BOUNCED = 'bounced', 'Bounced'
        COMPLAINED = 'complained', 'Marked as spam'

    direction = models.CharField(max_length=8, choices=Direction.choices)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='email_deliveries'
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        related_name='email_deliveries',
        null=True,
        blank=True,
    )
    participant = models.ForeignKey(
        Participant,
        on_delete=models.SET_NULL,
        related_name='email_deliveries',
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='email_deliveries',
        null=True,
        blank=True,
    )

    # The domain, not the address. See the class docstring.
    counterpart_domain = models.CharField(
        max_length=64, blank=True, default='', db_default=''
    )
    counterpart_hash = models.CharField(
        max_length=64, blank=True, default='', db_default=''
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.QUEUED,
        db_default=Status.QUEUED,
    )
    reason = models.CharField(max_length=120, blank=True, default='', db_default='')

    attempts = models.PositiveSmallIntegerField(default=0, db_default=0)

    provider = models.CharField(max_length=32, blank=True, default='', db_default='')
    # Indexed, NOT unique: a provider reports several statuses for one message
    # and they all carry this id.
    provider_message_id = models.CharField(
        max_length=128, blank=True, default='', db_default='', db_index=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # One outbound row per message per recipient, which is what makes a
            # resumed fan-out safe: the second attempt collides rather than
            # sending somebody the same message twice.
            models.UniqueConstraint(
                fields=['message', 'participant'],
                condition=models.Q(direction='outbound'),
                name='one_outbound_email_per_recipient',
            ),
        ]
        indexes = [
            models.Index(fields=['conversation', '-created_at']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f'{self.direction} {self.status} @{self.counterpart_domain}'
