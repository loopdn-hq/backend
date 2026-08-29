"""One-time passcodes for verifying an email address or a phone number.

EmailOtp and SmsOtp share every field and every rule, so the behaviour lives
on an abstract base and each concrete model only adds what is specific to its
channel. `related_name='%(class)s_profile'` expands to `emailotp_profile` and
`smsotp_profile`, so the reverse accessors are unchanged.

There is at most one live code per profile per channel: re-sending overwrites
the row rather than adding to it, so an old code stops working the moment a
new one goes out.
"""
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from otp.utils import default_expiry, generate_code
from user.models import Profile


class OtpError(Exception):
    """A code was refused. The message is safe to show the user.

    Deliberately not a DRF exception: this app should stay usable from a
    management command or a websocket consumer, not just from a view.
    """


class BaseOtp(models.Model):
    profile = models.OneToOneField(
        Profile,
        related_name='%(class)s_profile',
        on_delete=models.CASCADE,
    )
    code = models.CharField(max_length=6, null=False, blank=False)

    # Defaulted so that no caller can create a row that never expires by
    # simply forgetting the argument.
    expiry = models.DateTimeField(default=default_expiry)

    # Wrong guesses against the current code. Reset on every re-issue.
    attempts = models.PositiveSmallIntegerField(default=0)

    timestamp = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.__class__.__name__}<{self.profile.user.username}>'

    @property
    def is_expired(self):
        return timezone.now() >= self.expiry

    @property
    def attempts_exhausted(self):
        return self.attempts >= settings.OTP_MAX_ATTEMPTS

    @property
    def resend_available_at(self):
        return self.updated_at + settings.OTP_RESEND_COOLDOWN

    @property
    def seconds_until_resend(self):
        remaining = (self.resend_available_at - timezone.now()).total_seconds()
        return max(0, int(remaining))

    @classmethod
    def issue(cls, profile):
        """Create or replace this profile's code and return the row.

        Raises OtpError while the cooldown on the previous send is running.
        """
        existing = cls.objects.filter(profile=profile).first()
        if existing and existing.seconds_until_resend > 0:
            raise OtpError(
                f'Please wait {existing.seconds_until_resend} seconds '
                f'before requesting another code.'
            )

        otp, _ = cls.objects.update_or_create(
            profile=profile,
            defaults={
                'code': generate_code(),
                'expiry': default_expiry(),
                # A fresh code deserves a fresh budget of attempts.
                'attempts': 0,
            },
        )
        return otp

    def verify(self, code):
        """Consume the code, or raise OtpError explaining why not.

        The row is deleted on success: it is single-use, and keeping spent
        codes around only creates something to leak.
        """
        if self.is_expired:
            raise OtpError('That code has expired. Request a new one.')

        if self.attempts_exhausted:
            raise OtpError('Too many incorrect attempts. Request a new code.')

        # constant-time: a plain == leaks how many leading digits matched to
        # anyone who can measure the response.
        if not secrets.compare_digest(str(self.code), str(code).strip()):
            self.attempts += 1
            self.save(update_fields=['attempts', 'updated_at'])
            remaining = settings.OTP_MAX_ATTEMPTS - self.attempts
            if remaining <= 0:
                raise OtpError('Too many incorrect attempts. Request a new code.')
            raise OtpError(f'That code is incorrect. {remaining} attempts left.')

        self.delete()
        return True


class EmailOtp(BaseOtp):
    """Verifies the address on `profile.user.email`."""


class SmsOtp(BaseOtp):
    """Verifies a phone number.

    The number is stored on the row rather than read from the profile so that
    a code issued for one number cannot be used to verify another after the
    user edits their profile mid-flow.
    """

    phone_no = models.CharField(max_length=25, null=True, blank=True)
