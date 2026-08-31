"""Phone verification over SMS, via the Telnyx Verify API.

WHAT WE ARE DOING FOR SMS OTP
-----------------------------
Telnyx sells two different products that both end in "the user gets a text with
a code", and this project uses the second one:

  1. Messages API - Telnyx is a dumb pipe. We generate the code, store it,
     expire it, and check it ourselves. Requires owning a sending number.

  2. Verify API - Telnyx owns the whole code lifecycle. We say "verify this
     number", Telnyx generates the code, sends it, remembers it, and later
     answers "is 483920 correct?" with accepted or rejected.  <-- THIS ONE

So for SMS **we never see, generate or store the code**. That is why nothing
here calls `otp.utils.generate_code`, and why `SmsOtp.code` is left empty - see
the note in `otp/services.py`. The local `SmsOtp` row still exists, but only to
hold the rate-limit clock and to remember which number a code went to; it is no
longer the thing that decides whether a code is right.

Email OTP is unaffected and still uses `EmailOtp` end to end, so the code
generation, TTL, attempt cap and constant-time compare in `otp/models.py` are
all still live - just not for SMS.

Configuration is keyed on the environment, the same way the database, channel
layer and file storage are: with `TELNYX_API_KEY` unset, `send_verification_code`
logs instead of sending and `check_verification_code` accepts a fixed
development code, so a laptop and the test suite need no account, no credentials
and no network.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TELNYX_API_ROOT = 'https://api.telnyx.com/v2'

# Accepted by check_verification_code when Telnyx is not configured, so the
# whole flow is exercisable locally. It is only ever honoured when there is no
# API key, which a real deployment always has.
DEV_BYPASS_CODE = '000000'

# Sentinel for "caller said nothing", so that None stays a usable value for
# treat_http_error_as below.
_UNSET = object()


class SmsError(Exception):
    """Delivery or verification failed for a reason that is ours, not the user's.

    The message is safe to show. Deliberately not a DRF exception - this module
    has to stay usable from a management command or a Celery task, not just
    from a view.
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
    """
    allowed = settings.SMS_ALLOWED_COUNTRY_PREFIXES
    if not allowed:
        return True
    return any(phone_number.startswith(prefix) for prefix in allowed)


def send_verification_code(phone_number):
    """Ask Telnyx to generate a code and text it to this number.

    Returns the Telnyx verification id, or None when running unconfigured.
    Raises SmsError if Telnyx refused or could not be reached.
    """
    if not is_allowed_destination(phone_number):
        # Refused before the request is built: an unroutable destination should
        # never reach the provider, let alone be billed for.
        raise SmsError('We cannot send codes to that country.')

    if not _is_configured():
        logger.info(
            'SMS OTP (not sent - Telnyx not configured) to %s: use code %s',
            phone_number,
            DEV_BYPASS_CODE,
        )
        return None

    data = _post(
        f'{TELNYX_API_ROOT}/verifications/sms',
        {
            'phone_number': phone_number,
            'verify_profile_id': settings.TELNYX_VERIFY_PROFILE_ID,
        },
        failure_message='We could not send the code. Try again in a moment.',
    )

    verification_id = (data.get('data') or {}).get('id')
    logger.info('SMS OTP requested for %s (telnyx id %s)', phone_number, verification_id)
    return verification_id


def check_verification_code(phone_number, code):
    """Ask Telnyx whether this code is the one it sent to this number.

    Returns True or False. Raises SmsError only when Telnyx could not be
    reached - a wrong code is a False, not an exception, because it is an
    ordinary thing for a user to do.

    Telnyx validates the (number, code) pair together, which is what makes a
    code issued for one number useless for another.
    """
    if not _is_configured():
        return str(code).strip() == DEV_BYPASS_CODE

    # The number sits in the path, so it has to be escaped - a raw '+' is fine
    # in a path segment but quoting keeps this correct if the format ever
    # loosens.
    quoted = urllib.parse.quote(phone_number, safe='')

    data = _post(
        f'{TELNYX_API_ROOT}/verifications/by_phone_number/{quoted}/actions/verify',
        {
            'code': str(code).strip(),
            'verify_profile_id': settings.TELNYX_VERIFY_PROFILE_ID,
        },
        failure_message='We could not check that code. Try again in a moment.',
        # A rejected code comes back as a 4xx from some Telnyx profiles rather
        # than a 200 carrying response_code=rejected. Both mean "wrong code",
        # so an HTTP error here must not become a 500.
        treat_http_error_as=None,
    )

    if data is None:
        return False

    # Anything that is not an explicit acceptance is a rejection. Failing closed
    # matters more here than recognising every response shape.
    return (data.get('data') or {}).get('response_code') == 'accepted'


def _is_configured():
    return bool(settings.TELNYX_API_KEY and settings.TELNYX_VERIFY_PROFILE_ID)


def _post(url, payload, *, failure_message, treat_http_error_as=_UNSET):
    """POST JSON to Telnyx and return the decoded body.

    `treat_http_error_as` turns a 4xx into that value instead of an SmsError,
    which is how a rejected code stays an ordinary answer.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            'Authorization': f'Bearer {settings.TELNYX_API_KEY}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=settings.SMS_REQUEST_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Telnyx puts a human-readable reason in the body; worth logging, but
        # not worth showing a user who can do nothing with "10008".
        detail = exc.read().decode(errors='replace')[:500]
        logger.warning('Telnyx returned %s for %s: %s', exc.code, url, detail)
        if treat_http_error_as is not _UNSET:
            return treat_http_error_as
        raise SmsError(failure_message)
    except Exception as exc:
        # Timeout, DNS, TLS - anything meaning the request never landed.
        logger.error('Telnyx unreachable at %s: %s', url, exc)
        raise SmsError(failure_message)
