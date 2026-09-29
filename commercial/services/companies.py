"""Companies, and which one a person is working in.

docs/campaigns_final_plan.md, section 5. The active company is context, never
authorization: it decides which company the dashboard opens on, and nothing
else. Every company-scoped request still names its company and is looked up
through `Company.objects.manageable_by`.
"""
from django.db import transaction

from commercial.markets import default_currency
from commercial.models import CommercialPreference, Company


class CurrencyLocked(Exception):
    message = "The currency can't change once money has moved for this company."


#: What `update_company` may change. Never the owner, the status or the money:
#: those have their own paths (staff for status, the ledger for money).
EDITABLE_FIELDS = (
    'name',
    'industry',
    'country',
    'currency',
    'contact_email',
    'contact_phone',
    'website',
    'address',
    'description',
    'logo',
)


def create_company(
    owner,
    *,
    name,
    industry,
    country,
    contact_email,
    currency=None,
    website='',
    contact_phone='',
    address='',
    description='',
    logo=None,
):
    """Create a company for `owner` and make it the one they are working in.

    The owner is always the caller, whatever a request body says - which is
    why it is a positional argument the view fills from `request.user`, and
    never read from input. It starts active (plan decision 18).
    """
    with transaction.atomic():
        company = Company.objects.create(
            owner=owner,
            name=name,
            industry=industry,
            country=country,
            currency=currency or default_currency(country),
            contact_email=contact_email,
            contact_phone=contact_phone or '',
            website=website or '',
            address=address or '',
            description=description or '',
            logo=logo or '',
            status=Company.Status.ACTIVE,
        )
        _store_active(owner, company)
    return company


def update_company(company, **changes):
    """Change what the setup form set. Anything not in EDITABLE_FIELDS is refused.

    The currency is fixed once any money has moved: every ledger row, budget
    and balance is in it, and converting them is not something an edit form
    should do.
    """
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise TypeError(f'Not editable: {", ".join(sorted(unknown))}')

    currency = changes.get('currency')
    if currency and currency != company.currency and company.money_has_moved:
        raise CurrencyLocked()

    if 'logo' in changes and changes['logo'] is None:
        changes['logo'] = ''

    for field, value in changes.items():
        setattr(company, field, value)
    if changes:
        company.save(update_fields=[*changes, 'updated_at'])
    return company


def resolve_active_company(user):
    """The company this person's dashboard should open on, or None.

    The stored one if they can still manage it. Otherwise their most recently
    created company, stored from then on, so somebody with one company never
    has to choose. None when they have none, and the dashboard sends them to
    set one up.
    """
    stored_id = (
        CommercialPreference.objects.filter(user=user)
        .values_list('active_company_id', flat=True)
        .first()
    )
    companies = Company.objects.manageable_by(user).select_related('industry')

    if stored_id is not None:
        company = companies.filter(pk=stored_id).first()
        if company is not None:
            return company

    company = companies.order_by('-created_at', '-pk').first()
    if company is not None or stored_id is not None:
        # Stored even when None: a company they lost access to should not be
        # checked again on every visit.
        _store_active(user, company)
    return company


def set_active_company(user, company_id):
    """Switch the dashboard to one of this person's companies.

    Raises Company.DoesNotExist - a 404 to the caller - for a company they
    cannot manage, whether or not it exists, and stores nothing.
    """
    company = (
        Company.objects.manageable_by(user)
        .select_related('industry')
        .get(pk=company_id)
    )
    _store_active(user, company)
    return company


def _store_active(user, company):
    CommercialPreference.objects.update_or_create(
        user=user, defaults={'active_company': company}
    )
