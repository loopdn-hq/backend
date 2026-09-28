from django.db import models


class AdClick(models.Model):
    """Somebody followed a sponsored message's tracked link (plan 2b).

    Hashes, never the values: enough to dedupe and to recognise a link scanner,
    not enough to identify anybody.

    A `suspected_scanner` hit is recorded rather than dropped. A scanner that
    opens every URL in a text must not bill the advertiser for a click, but the
    count of them is how anybody notices a carrier has started scanning.
    """

    impression = models.ForeignKey(
        'ads.AdImpression', on_delete=models.CASCADE, related_name='clicks'
    )
    clicked_at = models.DateTimeField(auto_now_add=True)
    ip_hash = models.CharField(max_length=64, blank=True, default='', db_default='')
    user_agent_hash = models.CharField(max_length=64, blank=True, default='', db_default='')
    suspected_scanner = models.BooleanField(default=False, db_default=False)

    class Meta:
        ordering = ['-clicked_at']
        indexes = [
            # The dedupe lookup: this impression, from this address, recently.
            models.Index(fields=['impression', 'ip_hash'], name='ads_click_dedupe_idx'),
        ]

    def __str__(self):
        return f'click on {self.impression_id}'
