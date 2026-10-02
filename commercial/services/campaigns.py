"""Building a campaign: the draft, its audience and its creatives.

docs/campaigns_final_plan.md, sections 7 and 8. A draft may be incomplete - the
builder saves each step as it goes - and submit (services/transitions.py) is
the only gate. `submit_problems` is everything it checks.

Everything here changes a draft and nothing else. What review looks at is
locked once submitted (decision 6), so each change locks the campaign row and
refuses unless it is a draft: an edit racing a submit either lands before it,
and is reviewed, or is refused.

The views look every object up through `manageable_by(user)` and pass the
objects in, never ids, so nothing here can be pointed at somebody else's.
"""
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from commercial.models import (
    Campaign,
    CampaignEvent,
    Company,
    Creative,
    TargetingCriteria,
)
from commercial.services import pricing, reach, wallet
from commercial.services.errors import Invalid, Refused


class CampaignLocked(Refused):
    message = 'Only a draft can be changed.'


#: Decision 3: call and text need a tracking number per campaign (slice 8) and
#: stay hidden until then, though the model already knows them.
AVAILABLE_OBJECTIVES = (
    Campaign.Objective.WEBSITE,
    Campaign.Objective.LEAD,
    Campaign.Objective.BOOKING,
    Campaign.Objective.GROUP_JOIN,
)
OBJECTIVE_UNAVAILABLE = "Call and text campaigns aren't available yet."

#: Decision 4: chat is the only placement at first.
AVAILABLE_PLACEMENTS = (Campaign.Placement.CHAT,)

#: The button each objective's ads carry (section 7). One each, so a creative
#: may leave `cta_kind` out and get this one.
CTA_KIND_FOR = {
    Campaign.Objective.WEBSITE: Creative.CtaKind.LINK,
    Campaign.Objective.LEAD: Creative.CtaKind.LINK,
    Campaign.Objective.BOOKING: Creative.CtaKind.LINK,
    Campaign.Objective.GROUP_JOIN: Creative.CtaKind.GROUP_JOIN,
    Campaign.Objective.CALL: Creative.CtaKind.CALL,
    Campaign.Objective.TEXT: Creative.CtaKind.TEXT,
}

#: Enough to try a few versions of an ad; not somewhere to keep a library.
MAX_CREATIVES_PER_CAMPAIGN = 5

#: What `update_draft` may change. Never the company, the status or the
#: placement: the first is fixed at creation, the second is transitions.py's,
#: and the third has one value.
EDITABLE_FIELDS = (
    'name',
    'objective',
    'pacing',
    'total_budget_cents',
    'daily_budget_cents',
    'starts_at',
    'ends_at',
)

CREATIVE_FIELDS = ('headline', 'body', 'image', 'cta_label', 'cta_kind', 'cta_target')

_https_url = URLValidator(schemes=['https'])


# -- the draft -----------------------------------------------------------------


def create_draft(company, actor, *, objective, name=''):
    """Start a campaign for `company`: a draft, in chat, with only an objective.

    The [*] -> draft arrow of section 8, so it writes `created`.
    """
    _require_available(objective)
    with transaction.atomic():
        campaign = Campaign.objects.create(
            company=company,
            name=(name or '').strip(),
            objective=objective,
            placement=Campaign.Placement.CHAT,
            status=Campaign.Status.DRAFT,
        )
        CampaignEvent.record(campaign, CampaignEvent.Kind.CREATED, actor=actor)
    return campaign


def update_draft(campaign, **changes):
    """Save one builder step. Anything not in EDITABLE_FIELDS is a TypeError.

    Only the rules between fields that the database would refuse anyway are
    checked here; whether the whole thing is ready is submit's question.
    """
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise TypeError(f'Not editable: {", ".join(sorted(unknown))}')

    if 'objective' in changes:
        _require_available(changes['objective'])
    if 'name' in changes:
        changes['name'] = (changes['name'] or '').strip()

    with transaction.atomic():
        campaign = _lock_draft(campaign)
        errors = _budget_and_schedule_errors(
            **{
                field: changes.get(field, getattr(campaign, field))
                for field in (
                    'total_budget_cents',
                    'daily_budget_cents',
                    'starts_at',
                    'ends_at',
                )
            }
        )
        if errors:
            raise Invalid(errors)

        for field, value in changes.items():
            setattr(campaign, field, value)
        if changes:
            campaign.save(update_fields=[*changes, 'updated_at'])
    return campaign


def delete_draft(campaign):
    """Delete a draft that has never moved money. Its history goes with it."""
    with transaction.atomic():
        campaign = _lock_draft(campaign)
        if campaign.transactions.exists():
            raise Refused("This campaign has money history, so it can't be deleted.")
        campaign.delete()


# -- the audience ----------------------------------------------------------------


def set_targeting(campaign, **audience):
    """Save the audience step. Saving it at all is what marks the step done:
    an empty choice means "any", but submit needs the step to have been seen.
    """
    audience = reach.clean_audience(campaign.company, **audience)
    with transaction.atomic():
        campaign = _lock_draft(campaign)
        targeting, _ = TargetingCriteria.objects.update_or_create(
            campaign=campaign,
            defaults={
                'regions': audience['regions'],
                'min_group_size': audience['min_group_size'],
                'max_group_size': audience['max_group_size'],
            },
        )
        targeting.categories.set(audience['categories'])
    return targeting


# -- creatives ---------------------------------------------------------------------


def add_creative(campaign, **fields):
    """Add a whole creative. Unlike the campaign, a creative is never half-saved:
    the database requires a link button to have somewhere to go.
    """
    unknown = set(fields) - set(CREATIVE_FIELDS)
    if unknown:
        raise TypeError(f'Not a creative field: {", ".join(sorted(unknown))}')

    with transaction.atomic():
        campaign = _lock_draft(campaign)
        if campaign.creatives.count() >= MAX_CREATIVES_PER_CAMPAIGN:
            raise Refused(
                f'A campaign can have up to {MAX_CREATIVES_PER_CAMPAIGN} creatives.'
            )
        fields.setdefault('cta_kind', CTA_KIND_FOR[campaign.objective])
        fields['cta_target'] = (fields.get('cta_target') or '').strip()
        _require_valid_button(campaign.objective, fields['cta_kind'], fields['cta_target'])
        if fields.get('image') is None:
            fields['image'] = ''
        return Creative.objects.create(campaign=campaign, **fields)


def update_creative(creative, **changes):
    unknown = set(changes) - set(CREATIVE_FIELDS)
    if unknown:
        raise TypeError(f'Not a creative field: {", ".join(sorted(unknown))}')

    with transaction.atomic():
        campaign = _lock_draft(creative.campaign)
        if 'cta_target' in changes:
            changes['cta_target'] = (changes['cta_target'] or '').strip()
        _require_valid_button(
            campaign.objective,
            changes.get('cta_kind', creative.cta_kind),
            changes.get('cta_target', creative.cta_target),
        )
        if 'image' in changes and changes['image'] is None:
            changes['image'] = ''

        for field, value in changes.items():
            setattr(creative, field, value)
        if changes:
            creative.save(update_fields=[*changes, 'updated_at'])
    return creative


def delete_creative(creative):
    with transaction.atomic():
        _lock_draft(creative.campaign)
        creative.delete()


def button_errors(objective, cta_kind, cta_target):
    """What is wrong with a creative's button for this objective, as {field: message}.

    The button must fit the objective (section 7's table), and go somewhere
    safe: a link only to an https:// address, a group join only to Loopd'n.
    """
    if cta_kind != CTA_KIND_FOR.get(objective):
        return {'cta_kind': "This button doesn't fit the campaign's objective."}

    if cta_kind == Creative.CtaKind.LINK:
        try:
            _https_url(cta_target)
        except DjangoValidationError:
            return {'cta_target': 'Enter a web address that starts with https://.'}

    if cta_kind == Creative.CtaKind.GROUP_JOIN and not _is_loopdn_link(cta_target):
        return {'cta_target': "Enter the Loopd'n link to your group."}

    return {}


# -- submit ------------------------------------------------------------------------


def submit_problems(campaign):
    """Everything submit checks (section 7), as {step: message}. Empty means ready.

    Keyed by the field or step a builder sends the person back to: `name`,
    `audience`, `total_budget_cents`, `creatives` and so on. Every problem is
    named, not just the first, so one attempt shows the whole list.
    """
    problems = {}

    if campaign.company.status != Company.Status.ACTIVE:
        problems['company'] = "This company is suspended, so it can't submit campaigns."

    if not campaign.name.strip():
        problems['name'] = 'Give the campaign a name.'

    if campaign.objective not in AVAILABLE_OBJECTIVES:
        problems['objective'] = OBJECTIVE_UNAVAILABLE

    if campaign.placement not in AVAILABLE_PLACEMENTS:
        problems['placement'] = "This placement isn't available."

    targeting = (
        TargetingCriteria.objects.filter(campaign=campaign)
        .select_related('campaign__company')
        .first()
    )
    if targeting is None:
        problems['audience'] = 'Choose who should see it.'
    elif not reach.estimate_for(targeting)['available']:
        problems['audience'] = 'This audience is too small. Widen it to submit.'

    if not campaign.total_budget_cents:
        problems['total_budget_cents'] = 'Set a total budget.'
    elif campaign.company.balance_cents < campaign.total_budget_cents:
        # Checked again under the lock when submit reserves; this is so the
        # list of problems names it alongside everything else.
        problems['funds'] = wallet.InsufficientFunds(
            campaign.company, campaign.total_budget_cents
        ).message

    if pricing.rate_in_force(campaign.placement, campaign.company.currency) is None:
        problems['pricing'] = (
            f"Loopd'n isn't selling ads in {campaign.company.currency} yet."
        )
    problems.update(
        _budget_and_schedule_errors(
            total_budget_cents=campaign.total_budget_cents,
            daily_budget_cents=campaign.daily_budget_cents,
            starts_at=campaign.starts_at,
            ends_at=campaign.ends_at,
        )
    )

    if (
        campaign.pacing == Campaign.Pacing.STANDARD
        and campaign.ends_at is None
        and not campaign.daily_budget_cents
    ):
        problems['pacing'] = (
            '"Spread evenly" needs days to spread over: set an end date or a daily '
            'cap, or choose "As fast as possible".'
        )

    now = timezone.now()
    if campaign.starts_at is not None and campaign.starts_at < now:
        problems['starts_at'] = (
            'The start date has passed. Pick another, or start as soon as it is approved.'
        )
    if campaign.ends_at is not None and campaign.ends_at <= now:
        problems['ends_at'] = 'The end date has passed.'

    creatives = list(campaign.creatives.all())
    if not creatives:
        problems['creatives'] = 'Add at least one creative.'
    for creative in creatives:
        errors = button_errors(campaign.objective, creative.cta_kind, creative.cta_target)
        if errors:
            problems['creatives'] = f'"{creative.headline}": {next(iter(errors.values()))}'
            break

    return problems


# -- helpers -----------------------------------------------------------------------


def _lock_draft(campaign):
    """The campaign's row, locked for this transaction, if it is still a draft.

    Read fresh rather than trusted from the caller: the object a view loaded
    may have been submitted since, by another request.
    """
    campaign = (
        Campaign.objects.select_for_update()
        .select_related('company')
        .get(pk=campaign.pk)
    )
    if campaign.status == Campaign.Status.PENDING_REVIEW:
        raise CampaignLocked(
            'This campaign is waiting for review. Withdraw it to make changes.'
        )
    if campaign.status != Campaign.Status.DRAFT:
        raise CampaignLocked()
    return campaign


def _require_available(objective):
    if objective not in AVAILABLE_OBJECTIVES:
        raise Invalid({'objective': OBJECTIVE_UNAVAILABLE})


def _require_valid_button(objective, cta_kind, cta_target):
    errors = button_errors(objective, cta_kind, cta_target)
    if errors:
        raise Invalid(errors)


def _budget_and_schedule_errors(*, total_budget_cents, daily_budget_cents, starts_at, ends_at):
    """The rules between budget and schedule fields. Each is checked only when
    both of its fields are set, so a half-done step still saves.
    """
    errors = {}
    if (
        total_budget_cents is not None
        and daily_budget_cents is not None
        and daily_budget_cents > total_budget_cents
    ):
        errors['daily_budget_cents'] = "The daily cap can't be more than the total budget."
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        errors['ends_at'] = 'The campaign must end after it starts.'
    return errors


def _is_loopdn_link(target):
    """An address on the Loopd'n site itself: where a group's join page lives.

    Compared against FRONTEND_URL rather than a hard-coded domain, so staging
    and a laptop accept their own. The exact join path is checked when join
    links exist (slice 7 counts joins).
    """
    site = urlsplit(settings.FRONTEND_URL)
    try:
        parts = urlsplit(target)
    except ValueError:
        return False
    return (
        bool(parts.netloc)
        and parts.scheme == site.scheme
        and parts.netloc.lower() == site.netloc.lower()
        and not parts.username
    )
