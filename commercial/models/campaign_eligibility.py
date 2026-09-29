from django.db import models
from django.utils import timezone


class CampaignEligibility(models.Model):
    """Whether one campaign may appear in one group - and if not, why not.

    Written by selection (plan 1b), so "why did my campaign not run there" has
    an answer for Loopd'n staff. Never shown to a company: which groups a
    campaign could reach is exactly the membership disclosure §13 forbids, so
    there is deliberately no viewset for this table.

    Not called a "placement": in the ad industry, and in the Ads Manager README,
    that word means *where on screen* an ad appears.
    """

    campaign = models.ForeignKey(
        'commercial.Campaign', on_delete=models.CASCADE, related_name='eligibility'
    )
    # related_name='+': chat/ has no business walking from a group to the
    # campaigns that may target it.
    conversation = models.ForeignKey(
        'chat.Conversation', on_delete=models.CASCADE, related_name='+'
    )
    eligible_from = models.DateTimeField(default=timezone.now)
    # Blank means eligible. Otherwise the reason it is not, in plain words.
    suppressed_reason = models.CharField(max_length=120, blank=True, default='', db_default='')

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'campaign eligibility'
        constraints = [
            models.UniqueConstraint(
                fields=['campaign', 'conversation'],
                name='commercial_campaigneligibility_unique_campaign_conversation',
            ),
        ]

    def __str__(self):
        state = self.suppressed_reason or 'eligible'
        return f'{self.campaign_id} in {self.conversation_id}: {state}'
