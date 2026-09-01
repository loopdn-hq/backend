"""The verification flows, kept out of the HTTP layer.

Two channels, deliberately asymmetric because the providers are:

  EMAIL - we own the code. `EmailOtp` generates it, expires it, counts wrong
          guesses and compares it in constant time. Delivery is still not wired
          up; `start_email_verification` issues and logs, but sends nothing.

  SMS   - Telnyx owns the code. We use its Verify API, so this project never
          generates or stores an SMS code; it asks Telnyx to send one, and later
          asks Telnyx whether what the user typed was right. The `SmsOtp` row
          survives only to hold the resend clock, the local attempt count, and
          which number the code went to. See the header of utils/sms.py.

Both raise OtpError for anything the user should be told about;
loopd/exceptions.py turns that into a 400 inside the normal envelope.
"""
import logging

from django.conf import settings
from django.core.cache import cache

from otp.models import EmailOtp, OtpError, SmsOtp
from user.models import Profile
from utils.sms import (
    SmsError,
    check_verification_code,
    is_allowed_destination,
    send_verification_code,
)

logger = logging.getLogger(__name__)


def start_email_verification(user):
    """Issue a verification code for this user and return the EmailOtp row.

    Raises OtpError if a code was issued too recently.
    """
    otp = EmailOtp.issue(user.profile)

    # Until delivery exists, the console log is how the code reaches a
    # developer. Guarded on DEBUG so a live deployment never writes a valid
    # credential into its log files.
    if settings.DEBUG:
        logger.info('Email OTP for %s: %s', user.email or user.username, otp.code)

    return otp


def verify_email(user, code):
    """Check the code and activate the account. Raises OtpError if invalid."""
    otp = EmailOtp.objects.filter(profile=user.profile).first()
    if otp is None:
        raise OtpError('No verification code is pending. Request a new one.')

    # Raises on expiry, exhausted attempts or a wrong code; deletes the row
    # and returns True on success.
    otp.verify(code)

    if not user.is_active:
        user.is_active = True
        user.save(update_fields=['is_active'])

    return user


# --- phone ----------------------------------------------------------------


def start_phone_verification(user, phone_number=None):
    """Have Telnyx text a fresh code to this user's number.

    `phone_number` lets a caller verify a number that is not on the profile yet,
    which is what a change-my-number flow needs. Omitted, the number already on
    the profile is used.

    Raises OtpError for a cooldown, a spam ceiling or a missing number, and
    SmsError if Telnyx itself failed.
    """
    profile = user.profile
    number = (phone_number or profile.phone_number or '').strip()

    if not number:
        raise OtpError('Add a phone number to your profile first.')

    # Also checked in the serializer, but a number already on the profile never
    # passes through it. Raised as OtpError so that SmsError keeps meaning
    # exactly one thing: the provider failed.
    if not is_allowed_destination(number):
        raise OtpError('We cannot send codes to that country.')

    # Checked before issuing, so a request that is over the ceiling does not
    # also burn the resend cooldown.
    _enforce_send_limits(profile, number)

    # `code` is deliberately empty: under the Verify API there is no local code
    # to store. The row exists for the resend clock, the attempt count, and
    # `phone_no` - which is what a later verify is checked against, so a code
    # sent to one number can never confirm another.
    otp = SmsOtp.issue(profile, code='', phone_no=number)

    try:
        send_verification_code(number)
    except SmsError:
        # The provider failed, not the user. Dropping the row releases the
        # cooldown, so they can retry immediately instead of being locked out
        # for a minute over something that was our fault.
        otp.delete()
        raise

    _record_send(profile, number)
    return otp


def verify_phone(user, code):
    """Confirm the code with Telnyx and mark the number verified.

    Returns the Profile. Raises OtpError if the code is wrong, expired, or the
    attempt budget is spent.
    """
    profile = user.profile
    otp = SmsOtp.objects.filter(profile=profile).first()

    if otp is None:
        raise OtpError('No verification code is pending. Request a new one.')

    if otp.is_expired:
        raise OtpError('That code has expired. Request a new one.')

    if otp.attempts_exhausted:
        raise OtpError('Too many incorrect attempts. Request a new code.')

    # Telnyx is the judge - it validates the code against the number together,
    # which is what makes a code useless anywhere but the number it was sent to.
    # The local attempt count still runs, so brute force costs us no API calls
    # past the fifth guess.
    if not check_verification_code(otp.phone_no, code):
        otp.attempts += 1
        otp.save(update_fields=['attempts', 'updated_at'])
        remaining = settings.OTP_MAX_ATTEMPTS - otp.attempts
        if remaining <= 0:
            raise OtpError('Too many incorrect attempts. Request a new code.')
        raise OtpError(f'That code is incorrect. {remaining} attempts left.')

    number = otp.phone_no

    # Someone else may have claimed the number between the request and now.
    # Caught here so it reads as a message rather than a unique-constraint 409.
    if Profile.objects.filter(phone_number=number).exclude(pk=profile.pk).exists():
        raise OtpError('That phone number is already in use.')

    # Single-use: spent codes are only something to leak.
    otp.delete()

    # Both fields in ONE save. Profile.save() clears phone_no_verified whenever
    # the number changes - unless the same write sets both, which it documents
    # as the verification flow confirming a new number. Split these into two
    # saves and the flag is silently reset.
    profile.phone_number = number
    profile.phone_no_verified = True
    profile.save(update_fields=['phone_number', 'phone_no_verified', 'updated_at'])

    return profile


# --- spam ceilings ---------------------------------------------------------
#
# The resend cooldown on the model only stops bursts - waiting it out still buys
# 1,440 messages a day, and every one is a real charge. These are the ceilings
# that make that pointless.
#
# Counted per profile AND per destination number, because the two attacks are
# different: one account working through many numbers is pumping fraud, many
# accounts hammering one number is harassment. Either alone would miss one.
#
# Fixed windows in the cache rather than rolling windows in the database: an
# attacker gaining one extra send at a boundary does not matter, and this keeps
# the check to four cache reads.


def _send_limit_windows(profile, phone_number):
    hour = settings.OTP_MAX_SENDS_PER_HOUR
    day = settings.OTP_MAX_SENDS_PER_DAY
    return [
        (f'otp:sms:h:profile:{profile.pk}', 3600, hour),
        (f'otp:sms:d:profile:{profile.pk}', 86400, day),
        (f'otp:sms:h:number:{phone_number}', 3600, hour),
        (f'otp:sms:d:number:{phone_number}', 86400, day),
    ]


def _enforce_send_limits(profile, phone_number):
    for key, _ttl, limit in _send_limit_windows(profile, phone_number):
        if (cache.get(key) or 0) >= limit:
            # Deliberately vague: naming which ceiling was hit tells an attacker
            # which axis to spread across.
            raise OtpError(
                'Too many verification codes requested. Try again later.'
            )


def _record_send(profile, phone_number):
    for key, ttl, _limit in _send_limit_windows(profile, phone_number):
        # add() only writes when the key is absent, which is what starts the
        # window; incr() then bumps it without extending the expiry.
        cache.add(key, 0, ttl)
        try:
            cache.incr(key)
        except ValueError:
            # Expired between the add and the incr. Losing one tick of one
            # window is harmless - failing the send the user just paid for is not.
            cache.set(key, 1, ttl)
