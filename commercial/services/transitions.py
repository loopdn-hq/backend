"""Every change of a campaign's status: the arrows of section 8's diagram.

docs/campaigns_final_plan.md. One function per arrow. Each locks the campaign
row, checks the move is allowed from where the campaign is now, changes the
status, and writes exactly one CampaignEvent, all in one transaction. Nothing
else writes `Campaign.status`; creating a draft, the [*] -> draft arrow, is
services/campaigns.create_draft.

The owner's moves: submit and withdraw (slice 1), which move money since
slice 3 - submit fixes the price and sets the whole budget aside, withdraw
returns it - raising a running or paused campaign's budget (slice 3), and
pause, resume and cancel (slice 4). An admin's: approve and reject (slice 2,
from the admin site). The scheduler's: start and end on the campaign's dates
(slice 4, services/schedule.py).

A move that also moves money still writes ONE event: the money goes in that
event's `metadata` (what was reserved, released, at what price).
"""
from django.db import transaction
from django.utils import timezone

from commercial.models import Campaign, CampaignEvent, Creative
from commercial.services import campaigns, pricing, review, wallet
from commercial.services.errors import Invalid, Refused


class TransitionRefused(Refused):
    pass


class ReviewBlocked(Refused):
    """Approve refused: the automatic checks found a blocker (services/review.py)."""


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

        # A resubmitted campaign starts its review clean: the last reviewer's
        # notes stay in the history (the `rejected` event), not on the work.
        campaign.creatives.update(status=Creative.Status.PENDING_REVIEW, review_notes='')
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


def approve(campaign, actor, note=''):
    """pending_review -> approved: an admin passed it, and its creatives with it.

    Refused while the automatic checks find a blocker (services/review.py);
    the warnings are the reviewer's to weigh, and are recorded on the event.
    Its money stays reserved. It waits for its start date (slice 4).
    """
    with transaction.atomic():
        campaign = _lock(campaign)
        _require(
            campaign,
            Campaign.Status.PENDING_REVIEW,
            'Only a campaign waiting for review can be approved.',
        )

        found = review.findings(campaign)
        stopping = review.blockers(found)
        if stopping:
            raise ReviewBlocked(
                "It can't be approved yet: " + ' '.join(f.message for f in stopping)
            )

        campaign.creatives.update(status=Creative.Status.APPROVED, review_notes='')
        return _move(
            campaign,
            Campaign.Status.APPROVED,
            CampaignEvent.Kind.APPROVED,
            actor,
            note=(note or '').strip(),
            metadata={'warnings': [finding.code for finding in found]},
        )


def reject(campaign, actor, note, creative_notes=None):
    """pending_review -> draft, with the reviewer's reasons.

    `note` says why, to the company; it is required. `creative_notes`,
    {creative id: note}, marks the creatives that were the problem: they become
    `rejected` with their note, and the rest go back to `draft`. The reserved
    money returns to available and the price is forgotten, as on withdraw.
    """
    note = (note or '').strip()
    if not note:
        raise Invalid({'note': 'Say why, so the company knows what to fix.'})
    creative_notes = {
        int(pk): text.strip()
        for pk, text in (creative_notes or {}).items()
        if (text or '').strip()
    }

    with transaction.atomic():
        campaign = _lock(campaign)
        _require(
            campaign,
            Campaign.Status.PENDING_REVIEW,
            'Only a campaign waiting for review can be rejected.',
        )
        unknown = set(creative_notes) - set(campaign.creatives.values_list('pk', flat=True))
        if unknown:
            raise Invalid({'creatives': "Those creatives aren't this campaign's."})

        released = campaign.reserved_cents
        wallet.release(campaign)
        campaign.cpm_cents = None
        campaign.save(update_fields=['cpm_cents', 'updated_at'])

        campaign.creatives.exclude(pk__in=creative_notes).update(
            status=Creative.Status.DRAFT, review_notes=''
        )
        for pk, text in creative_notes.items():
            Creative.objects.filter(pk=pk).update(
                status=Creative.Status.REJECTED, review_notes=text
            )

        return _move(
            campaign,
            Campaign.Status.DRAFT,
            CampaignEvent.Kind.REJECTED,
            actor,
            note=note,
            metadata={
                'released_cents': released,
                'creatives': {str(pk): text for pk, text in creative_notes.items()},
            },
        )


#: Where cancel and end may come from: anything reviewed and not over.
STOPPABLE = (Campaign.Status.APPROVED, Campaign.Status.RUNNING, Campaign.Status.PAUSED)


def start(campaign):
    """approved -> running, by the scheduler once its start date has come.

    No start date means "as soon as it's approved". From here it may be
    served (slice 5); nothing else changes, and its money stays reserved.
    """
    with transaction.atomic():
        campaign = _lock(campaign)
        _require(campaign, Campaign.Status.APPROVED, 'Only an approved campaign can start.')
        now = timezone.now()
        if campaign.starts_at is not None and campaign.starts_at > now:
            raise TransitionRefused('Its start date has not come yet.')
        if _past_end(campaign, now):
            raise TransitionRefused('Its end date has passed; it ends instead.')
        return _move(campaign, Campaign.Status.RUNNING, CampaignEvent.Kind.STARTED, None)


def pause(campaign, actor):
    """running -> paused: the owner stops it for now. Nothing is served while
    it is paused, and its money stays reserved for when it resumes."""
    with transaction.atomic():
        campaign = _lock(campaign)
        _require(campaign, Campaign.Status.RUNNING, 'Only a running campaign can be paused.')
        return _move(campaign, Campaign.Status.PAUSED, CampaignEvent.Kind.PAUSED, actor)


def resume(campaign, actor):
    """paused -> running: the owner starts it again.

    Refused past its end date - the scheduler ends it - and with nothing left
    reserved to spend, which the owner fixes by adding budget first.
    """
    with transaction.atomic():
        campaign = _lock(campaign)
        _require(campaign, Campaign.Status.PAUSED, 'Only a paused campaign can be resumed.')
        if _past_end(campaign, timezone.now()):
            raise TransitionRefused('Its end date has passed, so it is ending.')
        if campaign.reserved_cents <= 0:
            raise TransitionRefused('Its budget is used up. Add budget to resume it.')
        return _move(campaign, Campaign.Status.RUNNING, CampaignEvent.Kind.RESUMED, actor)


def cancel(campaign, actor):
    """approved, running or paused -> ended: the owner stops it for good.

    Whatever it has reserved and not spent returns to available. Ended is
    final: to advertise again, start a new campaign.

    Allowed while running or paused as well as approved - section 8 of the
    plan lists approved only, but then a paused campaign with no end date
    would hold its money forever (decision 32). From slice 5, any views not
    yet settled are charged before the release.
    """
    with transaction.atomic():
        campaign = _lock(campaign)
        if campaign.status not in STOPPABLE:
            raise TransitionRefused(
                'Only an approved, running or paused campaign can be cancelled.'
            )
        return _finish(campaign, CampaignEvent.Kind.CANCELLED, actor)


def end(campaign):
    """approved, running or paused -> ended, by the scheduler: its end date
    has passed. Unspent money returns to available, as on cancel.

    An approved campaign can reach its end date without ever starting - one
    approved after it - and ends the same way, never having run.
    """
    with transaction.atomic():
        campaign = _lock(campaign)
        if campaign.status not in STOPPABLE:
            raise TransitionRefused('Only an approved, running or paused campaign can end.')
        if not _past_end(campaign, timezone.now()):
            raise TransitionRefused('Its end date has not come yet.')
        return _finish(campaign, CampaignEvent.Kind.ENDED, None)


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


def _past_end(campaign, now):
    return campaign.ends_at is not None and campaign.ends_at <= now


def _finish(campaign, kind, actor):
    """-> ended, returning what is reserved; one event saying how much."""
    was = campaign.status
    released = campaign.reserved_cents
    wallet.release(campaign)
    return _move(
        campaign,
        Campaign.Status.ENDED,
        kind,
        actor,
        metadata={'from': was, 'released_cents': released},
    )


def _move(campaign, status, kind, actor, *, note='', metadata=None):
    campaign.status = status
    campaign.save(update_fields=['status', 'updated_at'])
    CampaignEvent.record(campaign, kind, actor=actor, note=note, metadata=metadata)
    return campaign
