"""A sponsored card's button was pressed: count it, once (slice 6).

docs/campaigns_final_plan.md, section 11. The button's address is
/c/{token}/ (commercial/views.py), at the site root - not under /api/v1/, and
never containing "ads" - and this decides where it goes and what is recorded.

Where it goes: the creative's own destination, read from the database, never
from the token. Nothing about the viewer is added to it, and the redirect
sends no referrer. A genuine token always gets there; a forged one gets
nowhere.

What is recorded: every press, as an AdClick. One counts - the first press of
a card, fresh, from something that looks like a person. Repeats, link
scanners and preview fetches, and presses of a card older than
SPONSORED_CLICK_TOKEN_MAX_AGE_SECONDS are recorded and not counted, or (a
HEAD, a prefetch, a stale token) not recorded at all. A press can come before
the app has reported the view - a quick tap - and the press itself is proof
the card was seen, so the view is recorded first (measurement.record).

Clicks cost the company nothing: the price is per 1,000 views.
"""
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.crypto import salted_hmac

from commercial.models import AdClick, Creative
from commercial.services import measurement
from commercial.services.errors import Invalid
from commercial.tokens import read_click

#: The user agents of things that fetch links nobody pressed: crawlers,
#: preview cards, security scanners, scripts. A browser says none of these.
SCANNER = re.compile(
    r'bot|crawl|spider|slurp|preview|scan|curl|wget|python|java/|go-http|okhttp'
    r'|headless|phantom|facebookexternalhit|whatsapp|telegram|discord|skype|embedly',
    re.IGNORECASE,
)
#: Past this many presses of one card, more are not even recorded.
MAX_RECORDED_PER_CARD = 20


def follow(token, *, ip='', user_agent='', record=True, now=None):
    """Where the button goes, or None for a token that is not ours.

    `record=False` for a request nobody pressed (a HEAD, a prefetch): it is
    sent on its way and leaves no trace.
    """
    data, fresh = read_click(token)
    if data is None:
        return None
    creative = Creative.objects.filter(
        pk=data['creative_id'], campaign_id=data['campaign_id']
    ).first()
    if creative is None:
        return None
    destination = creative.cta_target or settings.FRONTEND_URL
    if record and fresh:
        _record(data, ip, user_agent, now)
    return destination


def _record(data, ip, user_agent, now):
    try:
        impression, _created = measurement.record(data, now)
    except Invalid:
        # Left the group since, or the campaign is gone: nothing to count on.
        return None
    if impression.clicks.count() >= MAX_RECORDED_PER_CARD:
        return None

    click = AdClick(
        impression=impression,
        ip_hash=_hash('ip', ip),
        user_agent_hash=_hash('ua', user_agent),
        suspected_scanner=looks_like_a_scanner(user_agent),
    )
    if not click.suspected_scanner:
        try:
            with transaction.atomic():
                click.counted = True
                click.save()
            return click
        except IntegrityError:
            # This card's click was counted already.
            click.pk = None
            click.counted = False
    click.save()
    return click


def looks_like_a_scanner(user_agent):
    return not user_agent or bool(SCANNER.search(user_agent))


def _hash(kind, value):
    if not value:
        return ''
    return salted_hmac(f'commercial.click.{kind}', value).hexdigest()
