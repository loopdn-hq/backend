from django.db import models
from django.db.models import Q
from django.utils import timezone


class AdImpression(models.Model):
    """One sponsored card one member actually saw: the billable view.

    docs/campaigns_final_plan.md, sections 10 and 11 (slice 5). Serving writes
    nothing; this row is written when the chat app reports that at least half
    the card was on screen for a second (decision 9), with the delivery token
    the serve signed. One row per token - `delivery_token` is the token's
    nonce, unique - so a retried report never counts twice.

    `billable` is decided when it is written: a view past the campaign's
    reservation or its daily cap is recorded and never charged. Settlement
    (services/settlement.py) charges the billable ones in bulk and stamps
    `settled_at`; until then a view is "spent, not yet settled".

    `participant` must never reach a company-facing serializer. It is the
    join from commercial data to a person; reports only aggregate over it.

    The links into chat are SET_NULL - not CASCADE, and not PROTECT. A group is
    hard-deleted when an admin deletes it for everyone: CASCADE would erase a
    billing record, and PROTECT would stop the admin deleting their own group.

    Rows are deleted once older than SPONSORED_VIEW_RETENTION_DAYS (decision
    15); CampaignStatsDaily keeps the totals for good.
    """

    campaign = models.ForeignKey(
        'commercial.Campaign', on_delete=models.PROTECT, related_name='impressions'
    )
    creative = models.ForeignKey(
        'commercial.Creative', on_delete=models.PROTECT, related_name='impressions'
    )
    # Campaign.Placement: where on screen it was seen.
    placement = models.CharField(max_length=16)
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
    # The signed token's nonce: what makes one serve one view at most.
    delivery_token = models.CharField(max_length=64, unique=True)

    billable = models.BooleanField(default=True, db_default=True)
    # Null until settlement has charged (or, if not billable, passed over) it.
    settled_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-viewed_at']
        indexes = [
            # A campaign's views by day: reports, roll-ups, today's count.
            models.Index(fields=['campaign', 'viewed_at'], name='commercial_impression_idx'),
            # What settlement has still to charge.
            models.Index(
                fields=['campaign'],
                condition=Q(settled_at__isnull=True),
                name='commercial_unsettled_idx',
            ),
        ]

    def __str__(self):
        return f'{self.campaign_id}: viewed {self.viewed_at:%Y-%m-%d %H:%M}'
