from django.db import models


class CampaignStatsDaily(models.Model):
    """One campaign's day, in totals: what reports read (slice 5).

    Rolled up from the raw rows every few minutes (services/stats.py) and kept
    for good, while AdImpression rows are deleted after the retention period
    (decision 15). Reports never read the raw rows.

    `day` is the calendar day in the company's time zone (markets.time_zone).
    `served` is how many times the card was handed out, from a counter - it
    is a gauge, not a bill. `viewed` is the billable unit, `reach` the
    distinct people who saw it that day, and `reach_to_date` the distinct
    people since the campaign started, because reach does not add up across
    days. `spend_cents` is what was charged that day, from the ledger.
    """

    campaign = models.ForeignKey(
        'commercial.Campaign', on_delete=models.CASCADE, related_name='daily_stats'
    )
    day = models.DateField()

    served = models.PositiveIntegerField(default=0, db_default=0)
    viewed = models.PositiveIntegerField(default=0, db_default=0)
    reach = models.PositiveIntegerField(default=0, db_default=0)
    reach_to_date = models.PositiveIntegerField(default=0, db_default=0)
    # Slices 6 and 7.
    clicks = models.PositiveIntegerField(default=0, db_default=0)
    results = models.PositiveIntegerField(default=0, db_default=0)
    spend_cents = models.PositiveIntegerField(default=0, db_default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['campaign', 'day']
        verbose_name_plural = 'campaign stats daily'
        constraints = [
            models.UniqueConstraint(
                fields=['campaign', 'day'], name='commercial_stats_one_row_per_day'
            ),
        ]

    def __str__(self):
        return f'{self.campaign_id} on {self.day}'
