"""Where a company may advertise, and the money it may pay in.

A market is a launch: pricing, tax and payment all have to work there first,
so adding one is a code change and a deploy, not a row an admin can add. That
is the difference from `Industry`, which is only a label.

Currency is chosen per company (docs/campaigns_final_plan.md, decision 11),
defaulting from the country, and every amount that company ever sees is in it.
Codes are ISO 4217, upper case. Stripe wants them lower case; convert at the
Stripe call, not here.
"""
from django.db import models


class Country(models.TextChoices):
    # ISO 3166-1 alpha-2.
    PHILIPPINES = 'PH', 'Philippines'
    UNITED_STATES = 'US', 'United States'


class Currency(models.TextChoices):
    PHP = 'PHP', 'Philippine peso'
    USD = 'USD', 'US dollar'


#: What a new company in each country starts with. It can pick the other one:
#: an agency in Manila may be billed in dollars.
DEFAULT_CURRENCY = {
    Country.PHILIPPINES: Currency.PHP,
    Country.UNITED_STATES: Currency.USD,
}


def default_currency(country):
    return DEFAULT_CURRENCY[Country(country)]
