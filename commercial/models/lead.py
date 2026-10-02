from django.conf import settings
from django.db import models


class Lead(models.Model):
    """What one member chose to send a company through a campaign's lead form
    (slice 7).

    A snapshot: the name, phone, email and message exactly as sent, and the
    consent sentence they were shown - never read back from the member's
    profile, so changing a profile later never changes a lead the company
    already has. The form is only pre-filled from the profile.

    Kept apart from the result it is counted as. The AdConversion
    (`conversion`) is the count - reports and cost per lead read it, and it
    holds nothing personal; this row is the contact details. Deleting a lead
    (a member's request, retention) leaves the company's counts and billing
    as they were.

    `member` is never shown to the company: it is there so one person sends
    one lead per campaign (the constraint), and so their leads can be found
    when they ask for them to be deleted. Nothing about the group the ad was
    seen in is kept here.
    """

    conversion = models.OneToOneField(
        'commercial.AdConversion', on_delete=models.CASCADE, related_name='lead'
    )
    campaign = models.ForeignKey(
        'commercial.Campaign', on_delete=models.PROTECT, related_name='leads'
    )
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )

    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=32, blank=True, default='', db_default='')
    email = models.EmailField(max_length=254, blank=True, default='', db_default='')
    message = models.CharField(max_length=500, blank=True, default='', db_default='')
    # The sentence the member agreed to by pressing Send, word for word.
    consent_text = models.CharField(max_length=300)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [
            models.UniqueConstraint(
                fields=['campaign', 'member'], name='commercial_lead_one_per_member'
            ),
        ]
        indexes = [
            models.Index(fields=['campaign', 'created_at'], name='commercial_lead_campaign_idx'),
        ]

    def __str__(self):
        return f'lead for {self.campaign_id}'
