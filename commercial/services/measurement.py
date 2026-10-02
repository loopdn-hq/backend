"""A served card was seen: record it, once (slice 5).

docs/campaigns_final_plan.md, section 11 and decision 9. The chat app reports
a view when at least half the card has been on screen for a second, with the
delivery token the serve signed. This checks the token (signature, age, that
the person reporting is the one it was served to, and is still in the group),
writes one AdImpression - one per token, so a retried report never counts
twice - and moves the counters.

Whether the view is billable is decided here, from the campaign as it is now:
it must still be running or paused, with views left in its budget and under
its daily cap. A view past either is recorded and never charged (section 9).
Charging is settlement's (settlement.py), in bulk, every few minutes.
"""
from django.db import IntegrityError, transaction
from django.utils import timezone

from chat.models import Participant
from commercial.models import AdImpression, Campaign, Creative
from commercial.services import counters, pacing
from commercial.services.errors import Invalid
from commercial.tokens import read_delivery

NOT_COUNTED = "That view can't be counted."


def record_view(viewer, token, now=None):
    """(impression, created). Raises Invalid for a token that names no view
    this person may report - forged, expired, someone else's - with one
    message for all of them."""
    data = read_delivery(token)
    if data is None or data['viewer_id'] != viewer.pk:
        raise Invalid({'token': NOT_COUNTED})
    return record(data, now)


def record(data, now=None):
    """(impression, created) for a served card's facts, from a token the
    caller has checked. A click can arrive before the view report (a quick
    tap): the click is proof enough the card was seen, so clicks.py records
    the view through here too. Raises Invalid if the viewer has left the
    group, or the campaign or creative is gone."""
    now = now or timezone.now()
    existing = AdImpression.objects.filter(delivery_token=data['nonce']).first()
    if existing is not None:
        return existing, False

    participant = (
        Participant.objects.filter(
            conversation_id=data['conversation_id'],
            user_id=data['viewer_id'],
            status=Participant.Status.ACTIVE,
        )
        .select_related('user__profile')
        .first()
    )
    campaign = (
        Campaign.objects.filter(pk=data['campaign_id']).select_related('company').first()
    )
    creative = Creative.objects.filter(
        pk=data['creative_id'], campaign_id=data['campaign_id']
    ).first()
    if participant is None or campaign is None or creative is None:
        raise Invalid({'token': NOT_COUNTED})

    try:
        with transaction.atomic():
            impression = AdImpression.objects.create(
                campaign=campaign,
                creative=creative,
                placement=data['placement'],
                conversation_id=data['conversation_id'],
                participant=participant,
                delivery_token=data['nonce'],
                billable=_billable(campaign, now),
                viewed_at=now,
            )
    except IntegrityError:
        # The same report twice at once: the other one wrote it.
        return AdImpression.objects.get(delivery_token=data['nonce']), False

    viewer = participant.user
    counters.count_view(
        impression, campaign.company.country, viewer.pk, counters.viewer_zone(viewer)
    )
    return impression, True


def _billable(campaign, now):
    if campaign.status not in (Campaign.Status.RUNNING, Campaign.Status.PAUSED):
        return False
    row = pacing.snapshot(campaign)
    if pacing.views_left(row) <= 0:
        return False
    cap = pacing.daily_cap_views(row)
    return cap is None or counters.views_today(campaign.pk, row['country'], now) < cap
