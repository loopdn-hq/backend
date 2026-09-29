from django.db import models


class AdImpression(models.Model):
    """One delivery of a sponsored message to one member on one channel.

    That is what "impression" means everywhere in this app: a delivery, not a
    render on screen. The Ads Manager README counts renders; this app does not,
    because SMS has no render event. It is the unit reach and delivered are
    counted in (plan 2a), and the unit a per-impression price is charged on.

    `participant` must never reach a company-facing serializer. It is the
    join from commercial data to a person; reports only aggregate over it.

    The links into chat are SET_NULL - not CASCADE, and not PROTECT. A group is
    hard-deleted when an admin deletes it for everyone: CASCADE would erase a
    billing record, and PROTECT would stop the admin deleting their own group.
    What survives is the creative, the cost and the time, which is enough to
    bill and report on and no longer points at anybody's conversation.
    """

    class Channel(models.TextChoices):
        # In the conversation, in the web and desktop apps. The only channel
        # anything writes before Stage 5.
        APP = 'app', 'App'
        # The `ads_sms` delivery channel, on its own numbers (plan, Stage 5).
        ADS_SMS = 'ads_sms', 'Sponsored SMS'

    creative = models.ForeignKey(
        'commercial.Creative', on_delete=models.PROTECT, related_name='impressions'
    )
    conversation = models.ForeignKey(
        'chat.Conversation',
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    participant = models.ForeignKey(
        'chat.Participant',
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    message = models.ForeignKey(
        'chat.Message',
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    channel = models.CharField(max_length=8, choices=Channel.choices)

    cost_cents = models.PositiveIntegerField(default=0, db_default=0)
    # Sponsored SMS only. Recognises a person for frequency capping without
    # storing their number beside commercial data, as SmsDelivery's does.
    counterpart_hash = models.CharField(max_length=64, blank=True, default='', db_default='')

    created_at = models.DateTimeField(auto_now_add=True)
    # An app impression is delivered when it is written; an SMS one when the
    # provider says so.
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # A retried serve must not bill twice for the same delivery. Rows
            # whose message or participant was deleted hold NULLs, which never
            # collide, so this cannot fail on history.
            models.UniqueConstraint(
                fields=['message', 'participant', 'channel'],
                name='commercial_adimpression_once_per_member_per_channel',
            ),
        ]
        indexes = [
            models.Index(fields=['creative', 'created_at'], name='commercial_impression_idx'),
        ]

    def __str__(self):
        return f'{self.creative_id} via {self.channel}'
