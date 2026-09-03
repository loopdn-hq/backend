"""Giving a group its phone number, and taking it away again.

THE RULES, which are product decisions and not implementation details:

  1. A group's number belongs to the COUNTRY OF WHOEVER CREATED IT. A Philippine
     admin's group gets a +63 number, so their members are texting a local
     number at local rates rather than dialling internationally to reach the
     family group.
  2. THE NUMBER NEVER CHANGES. Not by a member, not by an admin, not through the
     API. It is printed in people's phones as "Family" the moment anyone saves
     it, and a number that can be reassigned is a number that delivers a
     stranger's messages.
  3. DELETING THE GROUP DELETES THE NUMBER. It is released back to the provider
     so the lease stops being paid for, and the row goes with it.

WHERE THE PROVIDER LIVES
------------------------
Behind `order_number` and `release_number`, the only two functions here that
talk to anybody. Unconfigured they mint and discard a local placeholder, so
group creation works end to end on a laptop and in CI with no account.

One group, one number, bought when the group is created. Nothing is bought in
advance and nothing is reused between groups - `GroupNumber.number` is unique,
and a released number is never handed to a second group.

Two things about the provider are not optional: a search must precede every
order (it is refused otherwise), and an accepted order is not an owned number
until its status says success.
"""
import logging
import secrets
import time

from django.conf import settings
from django.db import transaction

from sms.client import (
    SmsError,
    find_owned_number,
    get_number_order,
    messaging_is_configured,
    release_owned_number,
    search_numbers,
)
from sms.client import order_number as order_number_at_provider
from sms.models import GroupNumber

logger = logging.getLogger(__name__)


def country_for(user):
    """Which country's number this person's group should get.

    Read from their verified phone number, because that is the only evidence
    the product has of where somebody actually is - a profile field they typed
    would be a guess, and an IP address is worse.

    Falls back to `SMS_DEFAULT_COUNTRY` when they have no verified number, which
    is a real case: a phone number is optional at registration, so somebody can
    create a group having never given one. Giving them a default country's
    number is better than refusing to create the group, but it IS a guess, and
    it is logged as one.
    """
    profile = getattr(user, 'profile', None)
    number = getattr(profile, 'phone_number', None) if profile else None
    verified = getattr(profile, 'phone_no_verified', False) if profile else False

    if number and verified:
        country = country_for_number(number)
        if country:
            return country

    logger.info(
        'No verified number for user %s; defaulting group number country to %s',
        getattr(user, 'pk', None),
        settings.SMS_DEFAULT_COUNTRY,
    )
    return settings.SMS_DEFAULT_COUNTRY


def country_for_number(e164):
    """The ISO country for an E.164 number, or None.

    A small explicit table rather than a phone-number library: this only has to
    know the countries the product actually serves, the same list the sending
    allowlist already uses, and a wrong answer here buys a number in the wrong
    country. Longest prefix wins, so +1 does not shadow a longer NANP entry if
    one is ever added.
    """
    number = (e164 or '').strip()
    best = None
    for prefix, country in settings.SMS_COUNTRY_BY_PREFIX.items():
        if number.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, country)
    return best[1] if best else None


def provision_for(conversation, creator):
    """Give this group its own number. Returns the GroupNumber, or None.

    Called after the group is committed, never inside the transaction that
    creates it: ordering a number is a call to somebody else's API, and a slow
    or failing provider must not turn "create a group" into "create nothing".
    A group briefly without a number works perfectly well in the app; a group
    that failed to be created does not.
    """
    existing = GroupNumber.objects.filter(conversation=conversation).first()
    if existing is not None:
        # Rule 2. Already has one, and it does not change.
        return existing

    country = country_for(creator)

    # A spare of the right country, if one is already leased. Locked, because
    # two groups created at the same instant must not claim the same number.
    with transaction.atomic():
        spare = (
            GroupNumber.objects.select_for_update(skip_locked=True)
            .filter(status=GroupNumber.Status.AVAILABLE, country=country)
            .first()
        )
        if spare is not None:
            spare.conversation = conversation
            spare.status = GroupNumber.Status.ACTIVE
            spare.save(update_fields=['conversation', 'status', 'updated_at'])
            return spare

    ordered, bought_in = _order_with_fallback(country)
    if ordered is None:
        logger.error(
            'Could not get a number for conversation %s in any country; it is '
            'app-only for now', conversation.pk,
        )
        return None

    return GroupNumber.objects.create(
        conversation=conversation,
        status=GroupNumber.Status.ACTIVE,
        number=ordered['number'],
        provider=ordered.get('provider', ''),
        provider_number_id=ordered.get('provider_number_id', ''),
        country=bought_in,
    )


def _order_with_fallback(country):
    """Buy in the creator's country if possible, otherwise anywhere we can.

    The creator's country is a preference, not a requirement. Some countries -
    the Philippines among them - need regulatory paperwork that no API call can
    complete, so insisting would leave those groups with no number at all. A
    group whose members text a foreign number works; a group with no number does
    not.

    Returns (order, country_actually_bought_in).
    """
    tried = []
    for candidate in [country] + list(settings.SMS_FALLBACK_COUNTRIES):
        if candidate in tried:
            continue
        tried.append(candidate)

        ordered = order_number(candidate)
        if ordered is not None:
            if candidate != country:
                logger.warning(
                    'No number available in %s; the group has a %s number instead',
                    country, candidate,
                )
            return ordered, candidate

    return None, country


def release_for(conversation):
    """Rule 3: the group is going, so its number goes with it.

    Released at the provider first, then the row is deleted. That order matters:
    a deleted row we never released is a number nobody is watching and everybody
    is still paying for, every month, with no record that it exists.
    """
    number = GroupNumber.objects.filter(conversation=conversation).first()
    if number is None:
        return

    if release_number(number):
        number.delete()
        return

    # The provider refused or could not be reached. The row is NOT deleted:
    # deleting it here is how a number ends up billed every month with nothing
    # in the system pointing at it and no way to find it again. It is detached
    # from the group that is going away and left in RELEASED, which is a state
    # nothing reassigns from - so it stays visible in the admin until somebody
    # releases it by hand.
    logger.error(
        'Could not release %s; keeping the record so it is not billed silently',
        number.number,
    )
    number.conversation = None
    number.status = GroupNumber.Status.RELEASED
    number.save(update_fields=['conversation', 'status', 'updated_at'])


# --- the two calls that reach the provider ---------------------------------


def order_number(country):
    """Buy a number in this country. Returns a dict, or None on failure.

    Search, then order, then confirm. All three steps are needed:

      SEARCH   Telnyx refuses an order for a number that was not returned by a
               search (error 85000), so this is not an optimisation.
      ORDER    Results are not reserved, so another customer can take the
               number in the gap. Several candidates are tried in turn rather
               than failing on the first collision.
      CONFIRM  A 200 means the order was accepted, not that we own anything.
               `status` is pending/success/failure, and the number is only ours
               when it says success.
    """
    if not messaging_is_configured():
        placeholder = _placeholder_number(country)
        logger.info(
            'No messaging provider configured; using placeholder number %s (%s)',
            placeholder, country,
        )
        return {'number': placeholder, 'provider': '', 'provider_number_id': ''}

    try:
        candidates = search_numbers(country, limit=settings.SMS_NUMBER_SEARCH_CANDIDATES)
    except SmsError:
        logger.exception('Could not search for a %s number', country)
        return None

    if not candidates:
        # Also what an unsupported country looks like: `filter[country_code]`
        # has no enum, so a country Telnyx does not sell in returns an empty
        # list rather than an error.
        logger.error('No SMS-capable numbers available in %s', country)
        return None

    for candidate in candidates:
        result = _buy(candidate, country)
        if result is not None:
            return result

    logger.error('Every candidate number in %s was taken before we could buy it', country)
    return None


def _buy(phone_number, country):
    """Order one specific number and confirm we own it. None if we do not."""
    try:
        order = order_number_at_provider(phone_number, reference=f'loopd-{country}')
    except SmsError:
        # Somebody else took it between the search and the order, or the order
        # was refused. Either way the next candidate is worth a try.
        logger.warning('Could not order %s; trying the next candidate', phone_number)
        return None

    data = (order or {}).get('data') or {}
    order_id = data.get('id')
    status = data.get('status')

    status = _await_completion(order_id, status)
    if status != 'success':
        # Money may already have been spent here. The order id is logged at
        # ERROR precisely so it can be found and finished by hand - a pending
        # order that nobody records is a number being paid for that no group
        # will ever use.
        logger.error(
            'Number order %s for %s ended as %s, not success; check it manually',
            order_id, phone_number, status,
        )
        return None

    owned = find_owned_number(phone_number)
    if owned is None:
        logger.error(
            'Bought %s but cannot find it in the account inventory; it cannot be '
            'released automatically', phone_number,
        )
        return {'number': phone_number, 'provider': 'telnyx', 'provider_number_id': ''}

    return {
        'number': phone_number,
        'provider': 'telnyx',
        # The id from the phone-number RESOURCE, not the one in the order.
        # DELETE only accepts this one.
        'provider_number_id': str(owned.get('id') or ''),
    }


def _await_completion(order_id, status):
    """Wait briefly for a pending order to settle. Returns the final status.

    US orders with no regulatory requirements settle immediately in practice,
    so this usually returns on the first check. It exists because the API
    contract does not promise that, and treating an accepted order as a
    completed one writes a number into the database that is not ours yet.
    """
    if status == 'success' or not order_id:
        return status

    for attempt in range(settings.SMS_NUMBER_ORDER_POLLS):
        time.sleep(settings.SMS_NUMBER_ORDER_POLL_SECONDS)
        try:
            refreshed = get_number_order(order_id)
        except SmsError:
            logger.warning('Could not poll number order %s', order_id)
            return status
        status = ((refreshed or {}).get('data') or {}).get('status')
        logger.info('Number order %s is %s (check %s)', order_id, status, attempt + 1)
        if status in ('success', 'failure'):
            return status
    return status


def release_number(group_number):
    """Hand a number back to the provider so the lease stops."""
    if not messaging_is_configured() or not group_number.provider_number_id:
        logger.info('Releasing number %s (no provider call needed)', group_number.number)
        return True

    try:
        release_owned_number(group_number.provider_number_id)
        return True
    except SmsError:
        # Logged loudly rather than raised: a provider being unreachable must
        # not stop somebody deleting their group, but a number nobody released
        # is billed every month with nothing pointing at it.
        logger.exception(
            'Could not release %s (provider id %s); release it by hand or it '
            'keeps being billed', group_number.number, group_number.provider_number_id,
        )
        return False


def _placeholder_number(country):
    """A syntactically valid, obviously fake number for local development.

    Random rather than sequential so that two groups created in one test run
    cannot collide on the unique column.
    """
    prefix = next(
        (p for p, c in settings.SMS_COUNTRY_BY_PREFIX.items() if c == country),
        '+1',
    )
    digits = ''.join(secrets.choice('0123456789') for _ in range(9))
    return f'{prefix}{digits}'
