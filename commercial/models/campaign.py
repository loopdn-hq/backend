from django.db import models
from django.db.models import F, Q


class CampaignQuerySet(models.QuerySet):
    def manageable_by(self, user):
        """The campaigns this person may see and change. Every lookup starts here.

        Whatever `Company.objects.manageable_by` allows, so the day several
        people manage one company (docs/rbac_plan.md, step 9) this follows
        without a change. Somebody else's campaign is a 404.
        """
        from commercial.models import Company

        return self.filter(company__in=Company.objects.manageable_by(user))


class Campaign(models.Model):
    """One ad campaign: what the company wants, what it may spend, and when.

    An *ad* campaign. Comments in chat/ use "campaign" for a carrier's A2P/10DLC
    registration, which is a different thing entirely: qualify the word in
    prose, and never let the two share a variable name in sms/.

    Selection (plan 1b) reads `status`, the dates and the budget; it never runs
    a campaign whose status is not `running`, so every way of stopping one -
    the company, a reviewer, an empty balance - is a status change.

    `status` is the truth, and only services/transitions.py writes it. A draft
    may be incomplete - the builder saves as it goes - so the name and the
    budget may be empty until submit, which is the only gate
    (docs/campaigns_final_plan.md, section 7).
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
        # Stopped by the company, by Loopd'n, or by running out of money.
        PAUSED = 'paused', 'Paused'
        ENDED = 'ended', 'Ended'

    class Placement(models.TextChoices):
        # Where the ad appears. A sponsored card in a group's chat is the only
        # one at first (plan decision 4), but it is a field from day one so the
        # group list or Discover later is a new choice, not a new mechanism.
        CHAT = 'chat', 'In group chats'

    class Pacing(models.TextChoices):
        # Spread the budget across the campaign's dates.
        STANDARD = 'standard', 'Spread evenly'
        # Spend as fast as the frequency caps allow.
        ACCELERATED = 'accelerated', 'As fast as possible'

    company = models.ForeignKey(
        'commercial.Company', on_delete=models.CASCADE, related_name='campaigns'
    )
    # Blank while it is a draft nobody has named yet. Submit requires one.
    name = models.CharField(max_length=120, blank=True, default='', db_default='')
    objective = models.CharField(max_length=16, choices=Objective.choices)
    placement = models.CharField(
        max_length=16,
        choices=Placement.choices,
        default=Placement.CHAT,
        db_default=Placement.CHAT,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_default=Status.DRAFT,
    )
    # "As fast as possible" by default: "spread evenly" needs days to spread
    # over - an end date, or a daily cap - and a new draft has neither.
    # Submit refuses "spread evenly" without one (services/campaigns.py).
    pacing = models.CharField(
        max_length=16,
        choices=Pacing.choices,
        default=Pacing.ACCELERATED,
        db_default=Pacing.ACCELERATED,
    )

    # In the company's currency. Null on a draft whose budget step is not done
    # yet; submit requires it.
    total_budget_cents = models.PositiveIntegerField(null=True, blank=True)
    # Null means no daily cap: pacing alone decides how the total is spent.
    daily_budget_cents = models.PositiveIntegerField(null=True, blank=True)

    # The price per 1,000 viewed ads, copied from the PricingRate in force
    # when it was submitted, so a later price change never alters a campaign
    # already bought. Null on a draft: withdrawing clears it, and resubmitting
    # takes the price in force then.
    cpm_cents = models.PositiveIntegerField(null=True, blank=True)
    # This campaign's share of the company's reserved money, not yet spent,
    # and what it has been charged. Only services/wallet.py writes them.
    reserved_cents = models.PositiveIntegerField(default=0, db_default=0)
    spent_cents = models.PositiveIntegerField(default=0, db_default=0)
    # Billable views settlement has counted (services/settlement.py). What is
    # charged is always billed_views x cpm_cents / 1000, rounded down, so a
    # price that is a fraction of a cent per view is still exact.
    billed_views = models.PositiveBigIntegerField(default=0, db_default=0)

    # Null while it is a draft nobody has scheduled yet.
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CampaignQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # Unset on a draft, but never zero: "no budget" is one state.
            models.CheckConstraint(
                condition=(
                    Q(total_budget_cents__isnull=True) | Q(total_budget_cents__gt=0)
                ),
                name='commercial_campaign_budget_positive',
            ),
            models.CheckConstraint(
                condition=(
                    Q(daily_budget_cents__isnull=True)
                    | Q(daily_budget_cents__lte=F('total_budget_cents'))
                ),
                name='commercial_campaign_daily_within_total',
            ),
            models.CheckConstraint(
                condition=(
                    Q(starts_at__isnull=True)
                    | Q(ends_at__isnull=True)
                    | Q(ends_at__gt=F('starts_at'))
                ),
                name='commercial_campaign_ends_after_it_starts',
            ),
        ]
        indexes = [
            # Selection's question: what is running and not yet over.
            models.Index(fields=['status', 'ends_at'], name='commercial_campaign_status_idx'),
        ]

    def __str__(self):
        return self.name or f'Untitled campaign #{self.pk}'
