"""What review checks automatically, before an admin decides.

docs/campaigns_final_plan.md, section 7 and slice 2. Each check is a Finding.
A BLOCKER means the campaign cannot be approved as it stands - approve refuses
while there is one (services/transitions.py). A WARNING is for the reviewer's
eye: something a person should look at, which may well be fine.

Cheap and local: no network calls (a link is not fetched), so the admin site
can show the findings on every open. A reach estimate is the one query that
costs anything.
"""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.utils import timezone

from commercial.models import Company, Creative, TargetingCriteria
from commercial.services import campaigns, reach

BLOCKER = 'blocker'
WARNING = 'warning'

# Seven or more digits, allowing the spaces, dots, dashes and brackets people
# write numbers with. A call or text must go through the campaign's tracking
# number (slice 8), so a number in the text routes around the measurement.
_PHONE = re.compile(r'(?:\+?\d[\s().-]?){7,}')
_LINK = re.compile(r'https?://|www\.', re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    #: Set when the finding is about one creative.
    creative_id: int | None = None


def findings(campaign):
    """Everything the automatic checks find, blockers first."""
    found = [*_campaign_findings(campaign)]
    for creative in campaign.creatives.order_by('created_at', 'pk'):
        found.extend(_creative_findings(campaign, creative))
    return sorted(found, key=lambda f: f.severity != BLOCKER)


def blockers(found):
    return [finding for finding in found if finding.severity == BLOCKER]


def _campaign_findings(campaign):
    now = timezone.now()

    if campaign.company.status != Company.Status.ACTIVE:
        yield Finding('company_suspended', BLOCKER, 'The company is suspended.')

    targeting = (
        TargetingCriteria.objects.filter(campaign=campaign)
        .select_related('campaign__company')
        .first()
    )
    if targeting is None:
        yield Finding('no_audience', BLOCKER, 'It has no audience.')
    elif not reach.estimate_for(targeting)['available']:
        yield Finding(
            'audience_too_small',
            BLOCKER,
            'Its audience is below the privacy minimums now.',
        )

    if (campaign.reserved_cents or 0) < (campaign.total_budget_cents or 0):
        yield Finding(
            'not_funded', BLOCKER, 'Its budget is not fully set aside from the balance.'
        )

    if campaign.ends_at is not None and campaign.ends_at <= now:
        yield Finding('ended', BLOCKER, 'Its end date has passed.')
    if campaign.starts_at is not None and campaign.starts_at < now:
        yield Finding(
            'start_passed',
            WARNING,
            'Its start date has passed: approved, it starts straight away.',
        )

    if not campaign.creatives.exists():
        yield Finding('no_creatives', BLOCKER, 'It has no creatives.')


def _creative_findings(campaign, creative):
    label = f'"{creative.headline}"'

    errors = campaigns.button_errors(campaign.objective, creative.cta_kind, creative.cta_target)
    if errors:
        yield Finding(
            'button', BLOCKER, f'{label}: {next(iter(errors.values()))}', creative.pk
        )

    text = f'{creative.headline}\n{creative.body}'
    if _PHONE.search(text):
        yield Finding(
            'phone_in_text',
            WARNING,
            f'{label} has a phone number in its text.',
            creative.pk,
        )
    if _LINK.search(text):
        yield Finding(
            'link_in_text', WARNING, f'{label} has a web address in its text.', creative.pk
        )

    if creative.cta_kind == Creative.CtaKind.LINK and campaign.company.website:
        site = _host(campaign.company.website)
        target = _host(creative.cta_target)
        if site and target and not (target == site or target.endswith(f'.{site}')):
            yield Finding(
                'unfamiliar_link',
                WARNING,
                f'{label} links to {target}, not the company\'s website ({site}).',
                creative.pk,
            )


def _host(address):
    host = urlsplit(address).hostname or ''
    return host.lower().removeprefix('www.')
