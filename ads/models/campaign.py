from django.db import models
from django.db.models import F, Q


class Campaign(models.Model):
    """One ad campaign: what the advertiser wants, what it may spend, and when.

    An *ad* campaign. Comments in chat/ use "campaign" for a carrier's A2P/10DLC
    registration, which is a different thing entirely: qualify the word in
    prose, and never let the two share a variable name in sms/.

    Selection (plan 1b) reads `status`, the dates and the budget; it never runs
    a campaign whose status is not `running`, so every way of stopping one -
    the advertiser, a reviewer, an empty balance - is a status change.
    """

    class Objective(models.TextChoices):
        # §11's list. Each has one result to count: `website` is an AdClick,
        # the other five are AdConversion rows with the same name.
        CALL = 'call', 'Phone calls'
        TEXT = 'text', 'Text messages'
        WEBSITE = 'website', 'Website visits'
        LEAD = 'lead', 'Leads'
        BOOKING = 'booking', 'Bookings'
        GROUP_JOIN = 'group_join', 'Group joins'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PENDING_REVIEW = 'pending_review', 'Pending review'
        # Passed review, waiting for `starts_at`.
        APPROVED = 'approved', 'Approved'
        RUNNING = 'running', 'Running'
        # Stopped by the advertiser, by Loopd'n, or by running out of money.
        PAUSED = 'paused', 'Paused'
        ENDED = 'ended', 'Ended'

    class Pacing(models.TextChoices):
        # Spread the budget across the campaign's dates.
        STANDARD = 'standard', 'Spread evenly'
        # Spend as fast as the frequency caps allow.
        ACCELERATED = 'accelerated', 'As fast as possible'

    advertiser = models.ForeignKey(
        'ads.Advertiser', on_delete=models.CASCADE, related_name='campaigns'
    )
    name = models.CharField(max_length=120)
    objective = models.CharField(max_length=16, choices=Objective.choices)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_default=Status.DRAFT,
    )
    pacing = models.CharField(
        max_length=16,
        choices=Pacing.choices,
        default=Pacing.STANDARD,
        db_default=Pacing.STANDARD,
    )

    total_budget_cents = models.PositiveIntegerField()
    # Null means no daily cap: pacing alone decides how the total is spent.
    daily_budget_cents = models.PositiveIntegerField(null=True, blank=True)

    # Null while it is a draft nobody has scheduled yet.
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=Q(total_budget_cents__gt=0),
                name='ads_campaign_has_a_budget',
            ),
            models.CheckConstraint(
                condition=(
                    Q(daily_budget_cents__isnull=True)
                    | Q(daily_budget_cents__lte=F('total_budget_cents'))
                ),
                name='ads_campaign_daily_within_total',
            ),
            models.CheckConstraint(
                condition=(
                    Q(starts_at__isnull=True)
                    | Q(ends_at__isnull=True)
                    | Q(ends_at__gt=F('starts_at'))
                ),
                name='ads_campaign_ends_after_it_starts',
            ),
        ]
        indexes = [
            # Selection's question: what is running and not yet over.
            models.Index(fields=['status', 'ends_at'], name='ads_campaign_status_idx'),
        ]

    def __str__(self):
        return self.name
