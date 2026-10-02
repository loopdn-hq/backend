"""Every change of a campaign's status: the arrows of section 8's diagram.

docs/campaigns_final_plan.md. One function per arrow. Each locks the campaign
row, checks the move is allowed from where the campaign is now, changes the
status, and writes exactly one CampaignEvent, all in one transaction. Nothing
else writes `Campaign.status`; creating a draft, the [*] -> draft arrow, is
services/campaigns.create_draft.

The owner's moves so far: submit and withdraw (slice 1), which move money
since slice 3 - submit fixes the price and sets the whole budget aside,
withdraw returns it - and raising a running or paused campaign's budget
(slice 3). Approve and reject are slice 2; start, pause, resume, cancel and
end are slice 4.

A move that also moves money still writes ONE event: the money goes in that
event's `metadata` (what was reserved, released, at what price).
"""
from django.db import transaction

from commercial.models import Campaign, CampaignEvent, Creative
from commercial.services import campaigns, pricing, wallet
from commercial.services.errors import Invalid, Refused


class TransitionRefused(Refused):
    pass


class SubmitRefused(Invalid):
    """Submit's checks failed. `errors` names every one (campaigns.submit_problems)."""


def submit(campaign, actor):
    """draft -> pending_review, once everything section 7 asks for is there.

    From here what review looks at is locked (decision 6): the creatives go to
    review with the campaign, and nothing in services/campaigns.py will change
    either until it is withdrawn or rejected.

    And the money (slice 3): the price in force is copied onto the campaign,
    and the whole budget moves from available to reserved, so it can never be
    promised to a second campaign. Short of money, nothing moves and submit
    says how much to add.
    """
    with transaction.atomic():
        campaign = _lock(campaign)
        _require(campaign, Campaign.Status.DRAFT, 'Only a draft can be submitted.')

        problems = campaigns.submit_problems(campaign)
        if problems:
            raise SubmitRefused(problems)

        rate = pricing.rate_in_force(campaign.placement, campaign.company.currency)
        campaign.cpm_cents = rate.cpm_cents
        campaign.save(update_fields=['cpm_cents', 'updated_at'])
        try:
            wallet.reserve(campaign, campaign.total_budget_cents)
        except wallet.InsufficientFunds as exc:
            # Another submit took the money since submit_problems looked.
            raise SubmitRefused({'funds': exc.message})

        campaign.creatives.update(status=Creative.Status.PENDING_REVIEW)
        return _move(
            campaign,
            Campaign.Status.PENDING_REVIEW,
            CampaignEvent.Kind.SUBMITTED,
            actor,
            metadata={
                'cpm_cents': campaign.cpm_cents,
                'reserved_cents': campaign.total_budget_cents,
            },
        )


def withdraw(campaign, actor):
    """pending_review -> draft: the owner takes it back to change something.

    Its reserved money returns to available, and its price is forgotten:
    resubmitting takes the price in force then.
    """
    with transaction.atomic():
        campaign = _lock(campaign)
        _require(
            campaign,
            Campaign.Status.PENDING_REVIEW,
            'Only a campaign waiting for review can be withdrawn.',
        )

        released = campaign.reserved_cents
        wallet.release(campaign)
        campaign.cpm_cents = None
        campaign.save(update_fields=['cpm_cents', 'updated_at'])

        campaign.creatives.update(status=Creative.Status.DRAFT)
        return _move(
            campaign,
            Campaign.Status.DRAFT,
            CampaignEvent.Kind.WITHDRAWN,
            actor,
            metadata={'released_cents': released},
        )


def increase_budget(campaign, actor, total_budget_cents):
    """Raise a running or paused campaign's total budget, reserving the difference.

    Only ever up (decision 6): what review approved is otherwise fixed, and a
    smaller budget would mean un-buying views already promised. The status
    does not change; the move is still one event, `budget_increased`.
    """
    with transaction.atomic():
        campaign = _lock(campaign)
        if campaign.status not in (Campaign.Status.RUNNING, Campaign.Status.PAUSED):
            raise TransitionRefused(
                "Only a running or paused campaign's budget can be raised."
            )
        current = campaign.total_budget_cents or 0
        if total_budget_cents <= current:
            raise Invalid({
                'total_budget_cents': 'The new budget must be more than the current one.'
            })

        try:
            wallet.reserve(campaign, total_budget_cents - current)
        except wallet.InsufficientFunds as exc:
            raise Invalid({'funds': exc.message})

        campaign.total_budget_cents = total_budget_cents
        campaign.save(update_fields=['total_budget_cents', 'updated_at'])
        CampaignEvent.record(
            campaign,
            CampaignEvent.Kind.BUDGET_INCREASED,
            actor=actor,
            metadata={
                'from_cents': current,
                'to_cents': total_budget_cents,
                'reserved_cents': total_budget_cents - current,
            },
        )
        return campaign


def _lock(campaign):
    # Fresh, not the caller's copy: two tabs submitting at once must not both
    # see a draft.
    return (
        Campaign.objects.select_for_update()
        .select_related('company')
        .get(pk=campaign.pk)
    )


def _require(campaign, status, refusal):
    if campaign.status != status:
        raise TransitionRefused(refusal)


def _move(campaign, status, kind, actor, *, note='', metadata=None):
    campaign.status = status
    campaign.save(update_fields=['status', 'updated_at'])
    CampaignEvent.record(campaign, kind, actor=actor, note=note, metadata=metadata)
    return campaign
