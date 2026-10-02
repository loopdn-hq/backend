"""Where a company may advertise, and the money it may pay in.

A market is a launch: pricing, tax and payment all have to work there first,
so adding one is a code change and a deploy, not a row an admin can add. That
is the difference from `Industry`, which is only a label.

Currency is chosen per company (docs/campaigns_final_plan.md, decision 11),
defaulting from the country, and every amount that company ever sees is in it.
Codes are ISO 4217, upper case. Stripe wants them lower case; convert at the
Stripe call, not here.
"""
from zoneinfo import ZoneInfo

from django.db import models

from utils import regions


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


#: Where a company's "day" is: a daily cap and a day of results run from
#: midnight to midnight here. One zone for the Philippines. The US has
#: several; New York stands in until a company can say which is theirs.
TIME_ZONE = {
    Country.PHILIPPINES: 'Asia/Manila',
    Country.UNITED_STATES: 'America/New_York',
}


def time_zone(country):
    return ZoneInfo(TIME_ZONE[Country(country)])


#: The least a company may add to its balance in one payment, in minor units.
#: Big enough that the card fee is not a large share of it. Stripe's own
#: minimum (about ₱30 / $0.50) is far below either.
MIN_TOP_UP_CENTS = {
    Currency.PHP: 50_000,  # ₱500
    Currency.USD: 1_000,  # $10
}

#: The most in one payment: Stripe's ceiling for both currencies.
MAX_TOP_UP_CENTS = 99_999_999


#: The regions a campaign may target in each country it may advertise in:
#: utils/regions.py's list - the same one a group declares its region from -
#: for each market. A campaign targets regions of its company's own country.
#: Choosing none means the whole country.
REGIONS = {country: regions.REGIONS[country] for country in Country}


def region_codes(country):
    """The codes a campaign in this country may target."""
    return regions.region_codes(Country(country))
