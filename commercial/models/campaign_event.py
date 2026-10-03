from django.conf import settings
from django.db import models


class CampaignEvent(models.Model):
    """One thing that happened to a campaign: who moved it, where, and when.

    History, never state (docs/campaigns_final_plan.md, principle 4):
    `Campaign.status` is the truth, and nothing reads this table to decide what
    a campaign may do. Every transition in services/transitions.py writes
    exactly one row, in the same transaction as the change it records, through
    `record` - so a status cannot change without its event, nor an event be
    written for a change that rolled back.

    Append-only. A row is never edited; `save` refuses an update.

    `actor` is SET_NULL with the name copied into `actor_label`, as on
    rbac.RoleAuditLog: deleting a person must not delete the record of what they
    did, and "withdrawn by <nobody>" would answer nothing. A null actor with a
    blank label is the system - the scheduler, the wallet - which is a
    different fact from "somebody since deleted".
    """

    class Kind(models.TextChoices):
        CREATED = 'created', 'Created'
        SUBMITTED = 'submitted', 'Submitted for review'
        WITHDRAWN = 'withdrawn', 'Withdrawn from review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        FUNDS_RESERVED = 'funds_reserved', 'Budget reserved'
        FUNDS_RELEASED = 'funds_released', 'Budget released'
        STARTED = 'started', 'Started'
        PAUSED = 'paused', 'Paused'
        RESUMED = 'resumed', 'Resumed'
        BUDGET_INCREASED = 'budget_increased', 'Budget increased'
        CANCELLED = 'cancelled', 'Cancelled'
        ENDED = 'ended', 'Ended'

    campaign = models.ForeignKey(
        'commercial.Campaign', on_delete=models.CASCADE, related_name='events'
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    #: Blank when the system did it.
    actor_label = models.CharField(max_length=150, blank=True, default='', db_default='')

    # Why, in words: a reviewer's reason for rejecting, a staff correction.
    note = models.TextField(blank=True, default='', db_default='')
    # The facts of the move that are not on the campaign any more, e.g. how
    # much was reserved. Never personal data about members.
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']
        indexes = [
            models.Index(fields=['campaign', 'created_at'], name='commercial_event_campaign_idx'),
        ]

    def __str__(self):
        who = self.actor_label or 'the system'
        return f'{self.campaign_id}: {self.kind} by {who}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('A campaign event is history and cannot be changed.')
        super().save(*args, **kwargs)

    @classmethod
    def record(cls, campaign, kind, *, actor=None, note='', metadata=None):
        """Write one row, copying the actor's name in.

        The single write path, so a caller cannot record a move and forget the
        label that keeps it readable after the account is gone.
        """
        return cls.objects.create(
            campaign=campaign,
            kind=kind,
            actor=actor,
            actor_label=getattr(actor, 'username', '') or '',
            note=note,
            metadata=metadata or {},
        )
