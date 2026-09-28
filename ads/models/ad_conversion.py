from django.db import models
from django.utils import timezone


class AdConversion(models.Model):
    """A result a campaign asked for: a call, a text, a lead, a booking or a
    group join. A website visit is an AdClick, not a conversion.

    `campaign` is required and `impression` is not. A call to a campaign's
    tracking number (plan 2c) belongs to the campaign but usually cannot be
    traced to one delivery - the caller may have seen the ad on somebody else's
    phone. When it can be, `impression` says which.
    """

    class Kind(models.TextChoices):
        # The same values as Campaign.Objective, minus `website`.
        CALL = 'call', 'Call'
        TEXT = 'text', 'Text'
        LEAD = 'lead', 'Lead'
        BOOKING = 'booking', 'Booking'
        GROUP_JOIN = 'group_join', 'Group join'

    campaign = models.ForeignKey(
        'ads.Campaign', on_delete=models.PROTECT, related_name='conversions'
    )
    impression = models.ForeignKey(
        'ads.AdImpression',
        on_delete=models.SET_NULL,
        related_name='conversions',
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # What the advertiser says the result was worth, when they say.
    value_cents = models.PositiveIntegerField(null=True, blank=True)
    # When it happened, which can be earlier than when a webhook told us.
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['campaign', 'kind'], name='ads_conversion_campaign_idx'),
        ]

    def __str__(self):
        return f'{self.kind} for {self.campaign_id}'
