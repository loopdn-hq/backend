"""Talking to Telnyx over HTTP: the transport both products share.

This project uses two different Telnyx products:

    sms/verification.py   Verify API   - proving a phone number belongs to someone
    sms/outbound.py       Messages API - sending group SMS from a number we own
                                         (not written yet; arrives with group SMS)

Everything they have in common is here: the API root, the bearer header, the
timeout, the error type, the destination allowlist, and `_post` itself. Keeping
one copy matters more than it looks - `_post` is the single seam every test in
the project mocks, and two copies would drift in exactly the ways that only show
up when real money is being spent.

WHY THIS IS NOT IN utils/
-------------------------
`utils/` is for helpers that belong to no single app. SMS now has an app, so the
provider client belongs to it.

WHAT MUST NOT HAPPEN HERE
-------------------------
This module, and the three beside it, must never import `sms.models`. They are
pure transport - `django.conf.settings`, the standard library and `cryptography`
only. `otp/services.py` and `loopd/exceptions.py` both import from here and both
load early; a model import would drag the app registry into that path and make
`otp`'s dependency on this app far heavier than it looks.

CONFIGURATION
-------------
Keyed on the environment, the same way the database, channel layer and file
storage are. Each product decides for itself whether it is configured - see
`_is_configured` in each module - so a deployment can run phone verification
without group SMS, or the other way round.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TELNYX_API_ROOT = 'https://api.telnyx.com/v2'

# Sentinel for "caller said nothing", so that None stays a usable value for
# treat_http_error_as below.
_UNSET = object()


class SmsError(Exception):
    """Delivery or verification failed for a reason that is ours, not the user's.

    The message is safe to show. Deliberately not a DRF exception - this module
    has to stay usable from a management command or a Celery task, not just
    from a view. loopd/exceptions.py turns it into a 503, exactly as it does
    for EmailError.

    There is exactly one of these, defined here and imported everywhere else. A
    second copy would be a different class, `except SmsError` would stop
    matching it, and a Telnyx outage would surface as a 500 instead of a 503.
    """


def is_allowed_destination(phone_number):
    """True when we are willing to send to this number's country.

    The single most effective control against SMS pumping: an attacker points
    verification at a premium-rate number in a country they control and collects
    a share of the termination fee. Every message is a real charge, so the
    cheapest defence is refusing to dial countries the product does not serve.

    An empty allowlist means "no restriction" - a deployment that genuinely
    serves everywhere should say so explicitly rather than have this silently
    drop traffic.

    Strictly this is policy rather than transport, but it is the pre-flight
    check both products run before a request is even built, so it lives beside
    the thing it guards.
    """
    allowed = settings.SMS_ALLOWED_COUNTRY_PREFIXES
    if not allowed:
        return True
    return any(phone_number.startswith(prefix) for prefix in allowed)


def _post(url, payload, *, failure_message, treat_http_error_as=_UNSET):
    """POST JSON to Telnyx and return the decoded body.

    `treat_http_error_as` turns a 4xx into that value instead of an SmsError,
    which is how a rejected verification code stays an ordinary answer.
    """
    return _request(
        'POST',
        url,
        payload=payload,
        failure_message=failure_message,
        treat_http_error_as=treat_http_error_as,
    )


def _request(method, url, *, payload=None, failure_message,
             treat_http_error_as=_UNSET, timeout=None):
    """One HTTP call to Telnyx, whatever the verb.

    Buying a number needs more than POST - a search is a GET, attaching a
    messaging profile is a PATCH, releasing is a DELETE - so the transport
    takes a method. Everything else stays identical, deliberately: one place
    that knows the auth header, the timeout, and how a Telnyx error is read.

    `timeout` overrides the default for calls that are not on a request path.
    Buying a number is slower than sending a text and does not have a person
    waiting on it, so it can afford to wait longer.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Authorization': f'Bearer {settings.TELNYX_API_KEY}'}
    if data is not None:
        headers['Content-Type'] = 'application/json'

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(
            request, timeout=timeout or settings.SMS_REQUEST_TIMEOUT
        ) as response:
            body = response.read()
            # DELETE and some PATCHes answer 204 with nothing at all, which is
            # a success and not a parse error.
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        # Telnyx puts a human-readable reason in the body; worth logging, but
        # not worth showing a user who can do nothing with "10008".
        detail = exc.read().decode(errors='replace')[:500]
        logger.warning('Telnyx returned %s for %s %s: %s', exc.code, method, url, detail)
        if treat_http_error_as is not _UNSET:
            return treat_http_error_as
        raise SmsError(failure_message)
    except Exception as exc:
        # Timeout, DNS, TLS - anything meaning the request never landed.
        logger.error('Telnyx unreachable at %s %s: %s', method, url, exc)
        raise SmsError(failure_message)


# --- Messages: sending arbitrary text from a number we own ------------------


def messaging_is_configured():
    return bool(settings.TELNYX_API_KEY and settings.TELNYX_MESSAGING_PROFILE_ID)


def send_text(*, to, from_number, text):
    """Send one text. Returns {'id', 'provider', 'segments'}.

    The single call the rest of the product makes to reach a phone. Per the
    product plan (§15, §20) the provider belongs to the telecom specialist, so
    this is the seam: everything above it knows only these three arguments and
    that dictionary, and swapping providers is a change to this function.

    With no provider configured it logs the fully rendered message and returns
    a synthetic id, exactly as `send_verification_code` does. That is what lets
    the whole fan-out - who receives, who is skipped and why, what it would
    cost - run end to end on a laptop and in CI with no account and no network.
    """
    if not is_allowed_destination(to):
        raise SmsError('We cannot send messages to that country.')

    if not messaging_is_configured():
        logger.info(
            'SMS (not sent - no messaging provider configured) %s -> %s: %s',
            from_number, to, text,
        )
        return {'id': '', 'provider': '', 'segments': 0}

    data = _post(
        f'{TELNYX_API_ROOT}/messages',
        {
            'from': from_number,
            'to': to,
            'text': text,
            'messaging_profile_id': settings.TELNYX_MESSAGING_PROFILE_ID,
        },
        failure_message='We could not send that message. Try again in a moment.',
    )

    payload = (data or {}).get('data') or {}
    parts = payload.get('parts')
    return {
        'id': payload.get('id') or '',
        'provider': 'telnyx',
        'segments': parts if isinstance(parts, int) else 0,
    }


# --- Numbers: finding one, buying it, giving it back -----------------------
#
# Every call here spends or stops spending money on the account. There is no
# sandbox and no dry-run flag on the order endpoint, so each one is real.


def search_numbers(country, *, limit=5):
    """SMS-capable numbers available to buy in this country.

    Returns a list of E.164 strings, best candidate first. More than one,
    deliberately: a search does NOT reserve anything, so another Telnyx
    customer can take a number in the gap between finding it and ordering it.
    The caller walks the list rather than failing on the first collision.
    """
    query = [
        ('filter[country_code]', country),
        ('filter[features][]', 'sms'),
        ('filter[phone_number_type]', 'local'),
        ('filter[limit]', str(limit)),
    ]
    if country in ('US', 'CA'):
        # Best-effort results are, per the spec, numbers that do NOT match the
        # criteria asked for. Left unset we could buy something outside the
        # locality we searched.
        query.append(('filter[best_effort]', 'false'))

    data = _request(
        'GET',
        f'{TELNYX_API_ROOT}/available_phone_numbers?{urllib.parse.urlencode(query)}',
        failure_message='We could not look for a phone number just now.',
        timeout=settings.SMS_NUMBER_REQUEST_TIMEOUT,
    )

    numbers = []
    for candidate in (data or {}).get('data') or []:
        # features is a list of OBJECTS, not strings: [{'name': 'sms'}, ...].
        # `'sms' in candidate['features']` is False for every number ever, and
        # would look exactly like "no numbers available".
        features = {f.get('name') for f in candidate.get('features') or []}
        if 'sms' in features and candidate.get('phone_number'):
            numbers.append(candidate['phone_number'])
    return numbers


def order_number(phone_number, *, reference=''):
    """Buy one specific number. Returns the order payload.

    `messaging_profile_id` goes at the TOP LEVEL of the body, as a sibling of
    `phone_numbers` - not inside the per-number objects. Nested, the order
    succeeds and the number arrives attached to no messaging profile, so it can
    neither send nor receive and nothing says why.
    """
    body = {'phone_numbers': [{'phone_number': phone_number}]}
    if settings.TELNYX_MESSAGING_PROFILE_ID:
        body['messaging_profile_id'] = settings.TELNYX_MESSAGING_PROFILE_ID
    if reference:
        body['customer_reference'] = reference[:64]

    # A successful order is HTTP 200, not 201. Nothing here asserts a status
    # code - urlopen raises for 4xx/5xx and anything else is success - which is
    # what stops a "201 expected" bug retrying and buying a SECOND number.
    logger.info('Ordering number %s (%s)', phone_number, reference or 'no reference')
    return _request(
        'POST',
        f'{TELNYX_API_ROOT}/number_orders',
        payload=body,
        failure_message='We could not buy a phone number just now.',
        timeout=settings.SMS_NUMBER_REQUEST_TIMEOUT,
    )


def get_number_order(order_id):
    """The current state of an order, for polling a pending one."""
    return _request(
        'GET',
        f'{TELNYX_API_ROOT}/number_orders/{urllib.parse.quote(str(order_id))}',
        failure_message='We could not check that number order.',
        timeout=settings.SMS_NUMBER_REQUEST_TIMEOUT,
    )


def find_owned_number(phone_number):
    """The account's own record for a number we bought, or None.

    This is the ONLY way to get the id that releases a number. The id inside an
    order response is a `number_order_phone_number` and DELETE will not take it
    - use that one and every release silently fails while the monthly charge
    continues.
    """
    for value in (phone_number, phone_number.lstrip('+')):
        # Telnyx documents that non-numerical characters in this filter yield no
        # results, so a leading '+' can return an empty set rather than an
        # error. Both forms are tried rather than guessed at.
        query = urllib.parse.urlencode([('filter[phone_number]', value)])
        data = _request(
            'GET',
            f'{TELNYX_API_ROOT}/phone_numbers?{query}',
            failure_message='We could not look up that phone number.',
            timeout=settings.SMS_NUMBER_REQUEST_TIMEOUT,
        )
        rows = (data or {}).get('data') or []
        if rows:
            return rows[0]
    return None


def release_owned_number(number_id):
    """Give a number back so the monthly charge stops."""
    logger.info('Releasing number id %s', number_id)
    _request(
        'DELETE',
        f'{TELNYX_API_ROOT}/phone_numbers/{urllib.parse.quote(str(number_id))}',
        failure_message='We could not release that phone number.',
        timeout=settings.SMS_NUMBER_REQUEST_TIMEOUT,
    )
    return True
