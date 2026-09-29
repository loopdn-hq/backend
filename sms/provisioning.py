"""Giving a group its phone number, and taking it away again.

THE RULES, which are product decisions and not implementation details:

  1. A group's number belongs to the COUNTRY OF WHOEVER CREATED IT, read from
     their VERIFIED phone number. A Philippine admin's group gets a +63 number,
     so their members are texting a local number at local rates rather than
     dialling internationally to reach the family group.
  1a. NO VERIFIED NUMBER MEANS NO SMS. There is no default country and no guess:
     the group is created, works normally in the app, and has its two SMS
     settings switched off. Guessing would buy a permanent foreign number
     (rule 2) that bills every member international rates forever, and the
     first anyone would know is the phone bill.
  1b. NO NUMBER IS A VALID ANSWER. If the country the group should have cannot
     be bought in, the group gets NO number - `conversation.sms_number` is null
     - rather than a number somewhere else. `SMS_FALLBACK_COUNTRIES` is empty
     by default for exactly this reason, and rule 2 is why: a consolation
     number in another country is permanent, and every member pays
     international rates to text it.
     The single exception is `SMS_FORCE_COUNTRY`, which names one country to buy
     every number in regardless of the creator. It is an operator's override,
     not a guess - set to 'US' on staging because Telnyx sells no SMS-capable
     number in most of the countries this product serves, and the feature would
     otherwise be untestable. Empty in production.
  1c. A NUMBER IS ONLY BOUGHT FOR A GROUP THAT USES SMS. Both `sms_delivery_enabled`
     and `sms_replies_enabled` off means nobody can send a text to this group
     and nobody can text it back, so a number would be a lease paid every month
     for a channel the creator explicitly switched off. Checked before the
     country, because "should we buy at all" comes before "where".
     Reversible: turning either setting on later is what
     `provision_missing_numbers` keys on, and it buys the number on its next run.
  2. THE NUMBER NEVER CHANGES. Not by a member, not by an admin, not through the
     API. It is printed in people's phones as "Family" the moment anyone saves
     it, and a number that can be reassigned is a number that delivers a
     stranger's messages.
  3. DELETING THE GROUP DELETES THE NUMBER. It is released back to the provider
     so the lease stops being paid for, and the row goes with it.

WHERE THE PROVIDER LIVES
------------------------
Behind `order_number` and `release_number`, the only two functions here that
talk to anybody. Unconfigured, they buy nothing and the group simply has no
number - `conversation.sms_number` is null. Nothing fabricates a number to
stand in for one, because a well-formed number that belongs to nobody is
indistinguishable from a real one everywhere it is later read: the API hands
it to a client, the client prints it, and somebody texts a dead line.

One group, one number, bought when the group is created - or, when that
failed, when an admin asks for it (`provision_requested`). Nothing is bought in
advance and nothing is reused between groups - `GroupNumber.number` is unique,
a released number is never handed to a second group, and
`GroupNumber.conversation` is one-to-one, so a group cannot hold two.

Two things about the provider are not optional: a search must precede every
order (it is refused otherwise), and an accepted order is not an owned number
until its status says success.
"""
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction

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
    """Which country's number this person's group should get, or None.

    Read from their verified phone number, because that is the only evidence
    the product has of where somebody actually is - a profile field they typed
    would be a guess, and an IP address is worse.

    None means "we do not know", and the caller must not turn that into a
    guess. Two cases reach it, and both are real: a phone number is optional at
    registration, so somebody can create a group having never given one; and a
    verified number in a country `SMS_COUNTRY_BY_PREFIX` does not map is a
    country we cannot buy in anyway.

    Returning a default country instead would buy a number that can never be
    changed (rule 2) in a country the group has nothing to do with, and every
    member would pay international rates to text it - discovered, at the
    earliest, on a phone bill.

    `SMS_FORCE_COUNTRY` overrides all of that, including rule 1a, and is the one
    sanctioned way to do so - see rule 1b in the module docstring. It exists
    because the countries this product serves cannot be bought in: it is set to
    'US' on staging so the feature can be exercised at all, and is empty in
    production, where rule 1 decides.
    """
    if settings.SMS_FORCE_COUNTRY:
        return settings.SMS_FORCE_COUNTRY

    profile = getattr(user, 'profile', None)
    number = getattr(profile, 'phone_number', None) if profile else None
    verified = getattr(profile, 'phone_no_verified', False) if profile else False

    if number and verified:
        return country_for_number(number)
    return None


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


def uses_sms(conversation):
    """Whether this group has any SMS leg switched on.

    Either one is enough: delivery alone is an announcement group people read
    by text, and replies alone is a group people text INTO. Both off is the
    only combination that needs no number.
    """
    return bool(
        conversation.sms_delivery_enabled or conversation.sms_replies_enabled
    )


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

    if not uses_sms(conversation):
        # Rule 1c. Both legs are off, so a number would be billed monthly for a
        # channel nobody can use. NOT _disable_sms: the settings already say
        # this, and writing them again would be a pointless UPDATE.
        logger.info(
            'Conversation %s has both SMS settings off, so no number was bought',
            conversation.pk,
        )
        return None

    country = country_for(creator)
    if country is None:
        # Rule 1a. Nothing to buy, because there is no country to buy in.
        _disable_sms(conversation)
        return None

    return _assign(conversation, country)


class NumberRefused(Exception):
    """An admin asked for a number and none can be had. The message says why,
    in words they can act on."""


# How long one request may hold a group's purchase. Longer than the slowest
# order - a search, several candidates and the completion polls - so the lock
# never lapses while a purchase it guards is still under way.
PROVISION_LOCK_SECONDS = 300


def provision_requested(conversation):
    """Buy the number this group should already have, because an admin asked.

    For a group whose number was never bought - the provider failed, had none,
    or was not configured when the group was created. Returns the GroupNumber;
    raises NumberRefused with the reason when there is none to be had.

    ONE GROUP, ONE NUMBER. A group that has a number gets it back and nothing
    is bought, so pressing the button twice is harmless. Two admins pressing it
    at once are serialised by a lock taken BEFORE the provider is called: the
    one-to-one column would refuse the second row, but only after the second
    number had been paid for - a lease nothing points at. `_assign` releases
    one if it ever gets that far anyway.

    UNLIKE `provision_for`, NOTHING IS SWITCHED OFF. Creation turns SMS off for
    a group whose creator has no country, silently, because nobody is there to
    tell. Here somebody is, so each rule that stops the purchase is a sentence
    for them instead, in the order that fixes it: the provider, the country,
    then the group's own settings.

    The country is still the CREATOR's (rule 1), not whoever pressed it. An
    admin in another country must not move the group's number abroad, and
    every member would pay for it.
    """
    existing = GroupNumber.objects.filter(conversation=conversation).first()
    if existing is not None:
        return existing

    lock = f'sms:provision:{conversation.pk}'
    if not cache.add(lock, True, PROVISION_LOCK_SECONDS):
        raise NumberRefused(
            'A number is already being set up for this Loop. Give it a minute.'
        )
    try:
        # Again inside the lock: the request that held it may have just
        # finished buying.
        existing = GroupNumber.objects.filter(conversation=conversation).first()
        if existing is not None:
            return existing

        if not messaging_is_configured():
            raise NumberRefused(
                "Texting isn't set up on this server yet, so no number can be "
                'bought.'
            )

        country = country_for(conversation.created_by)
        if country is None:
            raise NumberRefused(
                "A Loop's number is in its creator's country, and they have no "
                'verified phone number yet. Once they verify one, try again.'
            )

        if not uses_sms(conversation):
            raise NumberRefused(
                'Turn on SMS Delivery first - a number is only bought for a '
                'Loop that uses text.'
            )

        number = _assign(conversation, country)
        if number is None:
            raise NumberRefused(
                'No number could be bought right now. Try again in a few minutes.'
            )
        return number
    finally:
        cache.delete(lock)


def _assign(conversation, country):
    """A number of `country` for this group: a spare if one is leased, else
    one bought now. None if there is none to be had.

    The group's row is written in a savepoint, because the one-to-one column is
    the last word on "one number per group". If another request got there
    first, theirs stands: a spare we claimed is left spare, and a number we
    just bought is released at once rather than billed with nothing pointing
    at it.
    """
    # A spare of the right country, if one is already leased. Locked, because
    # two groups created at the same instant must not claim the same number.
    try:
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
    except IntegrityError:
        return GroupNumber.objects.filter(conversation=conversation).first()

    ordered, bought_in = _order_with_fallback(country)
    if ordered is None:
        logger.error(
            'Could not get a number for conversation %s in any country; it is '
            'app-only for now', conversation.pk,
        )
        return None

    bought = GroupNumber(
        conversation=conversation,
        status=GroupNumber.Status.ACTIVE,
        number=ordered['number'],
        provider=ordered.get('provider', ''),
        provider_number_id=ordered.get('provider_number_id', ''),
        country=bought_in,
    )
    try:
        with transaction.atomic():
            bought.save(force_insert=True)
    except IntegrityError:
        logger.error(
            'Conversation %s already had a number when %s was bought for it; '
            'releasing the new one', conversation.pk, bought.number,
        )
        release_number(bought)
        return GroupNumber.objects.filter(conversation=conversation).first()
    return bought


def _disable_sms(conversation):
    """Switch a group's SMS legs off, because it can never have a number.

    The group itself is untouched and works normally in the app - only the two
    SMS settings change, so they say what is true rather than advertising a
    channel that would accept messages and silently deliver nothing.

    Reversible on purpose. If the creator verifies a number later, an admin can
    turn delivery back on and `provision_missing_numbers` buys the group a
    number the next time it runs.

    `updated_at` is deliberately NOT in update_fields: it is auto_now and it
    orders the conversation list, so writing it here would push a group to the
    top as though somebody had posted in it.
    """
    logger.info(
        'Conversation %s: creator has no verified number, so SMS is off for it',
        conversation.pk,
    )
    conversation.sms_delivery_enabled = False
    conversation.sms_replies_enabled = False
    conversation.save(update_fields=['sms_delivery_enabled', 'sms_replies_enabled'])


def _order_with_fallback(country):
    """Buy in `country`, then in `SMS_FALLBACK_COUNTRIES` if that fails.

    THE LADDER IS EMPTY BY DEFAULT, so normally this orders in exactly one
    country and returns (None, country) if that does not work - rule 1b. The
    setting is kept because a fallback is occasionally the right call for a
    deliberate, temporary situation; it is not a default, because a consolation
    number in another country is permanent (rule 2) and bills every member
    international rates to text it.

    Returns (order, country_actually_bought_in).
    """
    if not messaging_is_configured():
        # Whether a provider exists is global, not per country, so no rung of
        # the ladder can succeed either. Checked once here rather than once per
        # rung, which logged the same line four times for a single group.
        logger.warning(
            'No messaging provider configured, so conversation numbers cannot '
            'be bought; the group has none',
        )
        return None, country

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
        # No account, so there is nothing to buy and nothing to pretend with.
        # The group is created and works in the app; it just has no number, the
        # same state as a provider that had none available.
        logger.warning(
            'No messaging provider configured, so no number was bought for %s; '
            'the group has none', country,
        )
        return None

    try:
        candidates = search_numbers(country, limit=settings.SMS_NUMBER_SEARCH_CANDIDATES)
    except SmsError:
        logger.exception('Could not search for a %s number', country)
        return None

    if not candidates:
        # NOT how an unsupported country looks. A country Telnyx has no SMS
        # coverage in answers HTTP 400 with error 10015 'No coverage found',
        # which `_request` raises as SmsError and the branch above catches -
        # true today of PH, GB, AU and SG, whose numbers are voice/fax only.
        # Reaching here means the country is sold in and is momentarily sold
        # out, which is why it is worth an error rather than a shrug.
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
