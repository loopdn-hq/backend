"""Phone verification over SMS, via the Telnyx Verify API.

WHAT WE ARE DOING FOR SMS OTP
-----------------------------
Telnyx sells two different products that both end in "the user gets a text", and
this project uses both, for different jobs:

  1. Verify API   - Telnyx owns the whole code lifecycle. We say "verify this
     number", Telnyx generates the code, sends it, remembers it, and later
     answers "is 483920 correct?" with accepted or rejected.  <-- THIS MODULE

  2. Messages API - Telnyx is a dumb pipe: we supply the text and a number we
     own, it delivers it. That is group SMS. It is not built yet; when it is, it
     lands in sms/outbound.py and shares the transport below.

So for the OTP **we never see, generate or store the code**. That is why nothing
here calls `otp.utils.generate_code`, and why `SmsOtp.code` is left empty - see
the note in `otp/services.py`. The local `SmsOtp` row still exists, but only to
hold the rate-limit clock and to remember which number a code went to; it is no
longer the thing that decides whether a code is right.

Email OTP is unaffected and still uses `EmailOtp` end to end, so the code
generation, TTL, attempt cap and constant-time compare in `otp/models.py` are
all still live - just not for SMS.

Configuration is keyed on the environment: with `TELNYX_API_KEY` or
`TELNYX_VERIFY_PROFILE_ID` unset, `send_verification_code` logs instead of
sending and `check_verification_code` accepts a fixed development code, so a
laptop and the test suite need no account, no credentials and no network.

The HTTP transport, the error type and the country allowlist are shared with the
Messages API and live in sms/client.py. Like that module, this one must never
import `sms.models` - `otp` imports it, and it has to stay a leaf.
"""
import logging
import urllib.parse

from django.conf import settings

from sms.client import TELNYX_API_ROOT, SmsError, _post, is_allowed_destination

logger = logging.getLogger(__name__)

# Accepted by check_verification_code when Telnyx is not configured, so the
# whole flow is exercisable locally. It is only ever honoured when there is no
# API key, which a real deployment always has.
DEV_BYPASS_CODE = '000000'


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
