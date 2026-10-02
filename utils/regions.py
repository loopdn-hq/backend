"""Where a place is: countries, and their first-level subdivisions.

ISO 3166. A country is its alpha-2 code ("PH"); a subdivision is its
ISO 3166-2 code ("PH-00", "US-CA") - and never anything finer, because a city
on a small group is not the aggregate fact the product plan (§13) allows.

Two things read this: a group saying where it is (chat), and a campaign
saying where it wants to be seen (commercial/markets.py, which narrows it to
the countries a company may advertise in). One list, so the region a group
declares is always one a campaign can choose.

The Philippines by region rather than province, which keeps each one large;
the US by state. Adding a country is a row in each table here.
"""

COUNTRIES = {
    'PH': 'Philippines',
    'US': 'United States',
}

REGIONS = {
    'PH': [
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
    'US': [
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
    """The subdivision codes of one country."""
    return {code for code, _ in REGIONS[country]}


def is_region(code):
    """Whether `code` is a place a group may declare: a country here, or one
    of its subdivisions."""
    country = code.split('-', 1)[0]
    if country not in COUNTRIES:
        return False
    return code == country or code in region_codes(country)


def region_name(code):
    """"Calabarzon, Philippines" - or the code itself, for one not listed."""
    country = code.split('-', 1)[0]
    country_name = COUNTRIES.get(country)
    if country_name is None:
        return code
    if code == country:
        return country_name
    name = dict(REGIONS[country]).get(code)
    return f'{name}, {country_name}' if name else code
