"""Loopd'n's own lead form (slice 7a): what a member chooses to send a company.

docs/campaigns_final_plan.md, decision 38. A lead campaign's button opens a
short form inside the app instead of an address. The form is pre-filled from
the member's profile - their name, and their phone and email if verified -
and they change or clear any of it before pressing Send. Only what they send
reaches the company, with the sentence they agreed to; nothing about who
else saw the ad, or the group they saw it in.

Opening the form is the button's press, so it counts as a click by
clicks.py's rules (once per card, never a scanner). Sending it is the result:
one AdConversion (the count, nothing personal) and one Lead (the details, a
snapshot) - one per member per campaign, which the database holds.

Both take the card's click token (commercial/tokens.py), and the person
asking must be the one it was served to, still in the group.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from commercial.models import AdConversion, Campaign, Creative, Lead
from commercial.services import clicks, measurement
from commercial.services.errors import Invalid, Refused
from commercial.tokens import read_click
from user.models import phone_number_validator

NOT_OURS = "That form can't be opened."
EXPIRED = 'This form has expired. Open the card again to send your details.'

#: A lead is taken while the campaign could have been on screen: running,
#: paused since, or ended since (the form a member already has open).
TAKING_LEADS = (Campaign.Status.RUNNING, Campaign.Status.PAUSED, Campaign.Status.ENDED)


def consent_for(company):
    """The sentence a member agrees to by pressing Send - stored with the lead."""
    return (
        f'{company.name} will receive what you send here, to get in touch with you '
        "about this. Loopd'n doesn't share anything else about you or your groups."
    )


def open_form(viewer, token, *, ip='', user_agent='', now=None):
    """The form for one card: who it goes to, what it is about, the member's
    details to start from, and whether they have sent it already. Counts the
    press as a click."""
    data = _checked(viewer, token)
    creative = _form_creative(data)
    clicks.record_press(data, ip=ip, user_agent=user_agent, now=now)
    company = creative.campaign.company
    return {
        'company_name': company.name,
        'headline': creative.headline,
        'fields': prefill(viewer),
        'consent': consent_for(company),
        'already_sent': Lead.objects.filter(campaign_id=data['campaign_id'], member=viewer).exists(),
    }


def send(viewer, token, *, name='', phone='', email='', message='', now=None):
    """Send what the member chose: a Lead and the AdConversion that counts it.

    Raises Invalid naming each field that is wrong, and Refused for a second
    lead to the same campaign or one that is no longer taking them.
    """
    now = now or timezone.now()
    data = _checked(viewer, token)
    creative = _form_creative(data)
    campaign = creative.campaign
    if campaign.status not in TAKING_LEADS:
        raise Refused("This campaign isn't taking details any more.")

    details = {
        'name': (name or '').strip(),
        'phone': (phone or '').strip(),
        'email': (email or '').strip(),
        'message': (message or '').strip(),
    }
    errors = _field_errors(details)
    if errors:
        raise Invalid(errors)

    impression, _created = measurement.record(data, now)
    company = campaign.company
    try:
        with transaction.atomic():
            conversion = AdConversion.objects.create(
                campaign=campaign,
                impression=impression,
                kind=AdConversion.Kind.LEAD,
                occurred_at=now,
            )
            return Lead.objects.create(
                conversion=conversion,
                campaign=campaign,
                member=viewer,
                consent_text=consent_for(company),
                **details,
            )
    except IntegrityError:
        raise Refused(f"You've already sent your details to {company.name} for this.")


def prefill(user):
    """Where the form starts: the member's name, and the phone and email
    Loopd'n has verified. Only a starting point - what is sent is what they
    leave in the form."""
    profile = getattr(user, 'profile', None)
    name = (profile.display_name if profile else '') or user.get_full_name() or user.username
    return {
        'name': name,
        'phone': profile.phone_number if profile and profile.phone_no_verified else '',
        'email': user.email if profile and profile.email_verified else '',
    }


def _checked(viewer, token):
    data, fresh = read_click(token)
    if data is None or data['viewer_id'] != viewer.pk:
        raise Invalid({'token': NOT_OURS})
    if not fresh:
        raise Invalid({'token': EXPIRED})
    return data


def _form_creative(data):
    creative = (
        Creative.objects.filter(
            pk=data['creative_id'],
            campaign_id=data['campaign_id'],
            cta_kind=Creative.CtaKind.LEAD_FORM,
        )
        .select_related('campaign__company')
        .first()
    )
    if creative is None:
        raise Invalid({'token': NOT_OURS})
    return creative


def _field_errors(details):
    errors = {}
    if not details['name']:
        errors['name'] = 'Enter your name.'
    if not details['phone'] and not details['email']:
        errors['phone'] = 'Give a phone number or an email address, so they can reach you.'
    if details['email']:
        try:
            validate_email(details['email'])
        except DjangoValidationError:
            errors['email'] = 'Enter a valid email address.'
    if details['phone']:
        try:
            phone_number_validator(details['phone'])
        except DjangoValidationError as exc:
            errors['phone'] = exc.messages[0]
    return errors
