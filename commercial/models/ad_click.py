from django.db import models
from django.db.models import Q


class AdClick(models.Model):
    """Somebody pressed a sponsored card's button (slice 6).

    Written by GET /c/{token}/ (services/clicks.py) on the way to the
    destination. Every press is recorded; `counted` marks the one that counts:
    the first press of a card that does not look like a link scanner or a
    preview fetch. Repeats and scanners are kept rather than dropped - a
    count of them is how anybody notices a scanner - but never reported as
    clicks. One counted click per card at most (the constraint below).

    Hashes, never the values: enough to tell repeats and scanners apart, not
    enough to identify anybody.
    """

    impression = models.ForeignKey(
        'commercial.AdImpression', on_delete=models.CASCADE, related_name='clicks'
    )
    clicked_at = models.DateTimeField(auto_now_add=True)
    ip_hash = models.CharField(max_length=64, blank=True, default='', db_default='')
    user_agent_hash = models.CharField(max_length=64, blank=True, default='', db_default='')
    suspected_scanner = models.BooleanField(default=False, db_default=False)
    counted = models.BooleanField(default=False, db_default=False)

    class Meta:
        ordering = ['-clicked_at']
        constraints = [
            models.UniqueConstraint(
                fields=['impression'],
                condition=Q(counted=True),
                name='commercial_click_counted_once',
            ),
        ]
        indexes = [
            # The dedupe lookup: this impression, from this address, recently.
            models.Index(fields=['impression', 'ip_hash'], name='commercial_click_dedupe_idx'),
            # Reports: a campaign's counted clicks by day.
            models.Index(
                fields=['clicked_at'],
                condition=Q(counted=True),
                name='commercial_click_counted_idx',
            ),
        ]

    def __str__(self):
        return f'click on {self.impression_id}'
