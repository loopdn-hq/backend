"""The email-verification flow, kept out of the HTTP layer.

Delivery is not wired up yet: `start_email_verification` issues the code and
logs it, but sends nothing. When email is switched on, that is the one
function that changes - callers and tests stay as they are.
"""
import logging

from django.conf import settings

from otp.models import EmailOtp, OtpError

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
