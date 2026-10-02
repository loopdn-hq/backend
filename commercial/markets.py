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


#: The least a company may add to its balance in one payment, in minor units.
#: Big enough that the card fee is not a large share of it. Stripe's own
#: minimum (about ₱30 / $0.50) is far below either.
MIN_TOP_UP_CENTS = {
    Currency.PHP: 50_000,  # ₱500
    Currency.USD: 1_000,  # $10
}

#: The most in one payment: Stripe's ceiling for both currencies.
MAX_TOP_UP_CENTS = 99_999_999


#: The regions a campaign may target in each country: ISO 3166-2 first-level
#: subdivisions, and never anything finer - a city on a small group is not the
#: aggregate fact §13 allows. The Philippines by region rather than province,
#: which keeps each one large; the US by state.
#:
#: A campaign targets regions of its company's own country (the market it
#: advertises in). Choosing none means the whole country.
REGIONS = {
    Country.PHILIPPINES: [
        ('PH-00', 'Metro Manila (NCR)'),
        ('PH-15', 'Cordillera (CAR)'),
        ('PH-01', 'Ilocos'),
        ('PH-02', 'Cagayan Valley'),
        ('PH-03', 'Central Luzon'),
        ('PH-40', 'Calabarzon'),
        ('PH-41', 'Mimaropa'),
        ('PH-05', 'Bicol'),
        ('PH-06', 'Western Visayas'),
        ('PH-07', 'Central Visayas'),
        ('PH-08', 'Eastern Visayas'),
        ('PH-09', 'Zamboanga Peninsula'),
        ('PH-10', 'Northern Mindanao'),
        ('PH-11', 'Davao'),
        ('PH-12', 'Soccsksargen'),
        ('PH-13', 'Caraga'),
        ('PH-14', 'Bangsamoro (BARMM)'),
    ],
    Country.UNITED_STATES: [
        ('US-AL', 'Alabama'),
        ('US-AK', 'Alaska'),
        ('US-AZ', 'Arizona'),
        ('US-AR', 'Arkansas'),
        ('US-CA', 'California'),
        ('US-CO', 'Colorado'),
        ('US-CT', 'Connecticut'),
        ('US-DE', 'Delaware'),
        ('US-DC', 'District of Columbia'),
        ('US-FL', 'Florida'),
        ('US-GA', 'Georgia'),
        ('US-HI', 'Hawaii'),
        ('US-ID', 'Idaho'),
        ('US-IL', 'Illinois'),
        ('US-IN', 'Indiana'),
        ('US-IA', 'Iowa'),
        ('US-KS', 'Kansas'),
        ('US-KY', 'Kentucky'),
        ('US-LA', 'Louisiana'),
        ('US-ME', 'Maine'),
        ('US-MD', 'Maryland'),
        ('US-MA', 'Massachusetts'),
        ('US-MI', 'Michigan'),
        ('US-MN', 'Minnesota'),
        ('US-MS', 'Mississippi'),
        ('US-MO', 'Missouri'),
        ('US-MT', 'Montana'),
        ('US-NE', 'Nebraska'),
        ('US-NV', 'Nevada'),
        ('US-NH', 'New Hampshire'),
        ('US-NJ', 'New Jersey'),
        ('US-NM', 'New Mexico'),
        ('US-NY', 'New York'),
        ('US-NC', 'North Carolina'),
        ('US-ND', 'North Dakota'),
        ('US-OH', 'Ohio'),
        ('US-OK', 'Oklahoma'),
        ('US-OR', 'Oregon'),
        ('US-PA', 'Pennsylvania'),
        ('US-RI', 'Rhode Island'),
        ('US-SC', 'South Carolina'),
        ('US-SD', 'South Dakota'),
        ('US-TN', 'Tennessee'),
        ('US-TX', 'Texas'),
        ('US-UT', 'Utah'),
        ('US-VT', 'Vermont'),
        ('US-VA', 'Virginia'),
        ('US-WA', 'Washington'),
        ('US-WV', 'West Virginia'),
        ('US-WI', 'Wisconsin'),
        ('US-WY', 'Wyoming'),
    ],
}


def region_codes(country):
    """The codes a campaign in this country may target."""
    return {code for code, _ in REGIONS[Country(country)]}
