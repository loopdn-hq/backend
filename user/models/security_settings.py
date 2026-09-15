"""Settings → Security."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class SecuritySettings(UserSettings):
    """How an account is protected at sign-in.

    NOTHING READS THESE YET. Sign-in does not ask for a second code, and no
    alert is sent; the web screen shows both as "Not available yet".

    NOT HERE, ON PURPOSE - these already exist:

    - The password is Django's own (`User.password`).
    - One-time codes are `otp.EmailOtp` and `otp.SmsOtp`. Two-factor should send
      and check its codes through them rather than a new code model. Note that
      each is one row per Profile, so a sign-in code and a verification code in
      flight at once would share that row - decide that before building it.
    - Active sessions are simplejwt's `OutstandingToken` and `BlacklistedToken`
      (`rest_framework_simplejwt.token_blacklist` is installed). Signing a
      device out is blacklisting its refresh token. A password change does not
      do that today, which the Security screen says.
    """

    class TwoFactorMethod(models.TextChoices):
        SMS = 'sms', 'Text message'
        EMAIL = 'email', 'Email'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='security_settings',
    )

    two_factor_enabled = models.BooleanField(default=False, db_default=False)

    # Where sign-in codes go. Blank until two-factor is set up. Only a VERIFIED
    # phone number or email address may be chosen: a code sent to an unproven
    # address is a code sent to whoever typed that address in.
    two_factor_method = models.CharField(
        max_length=5,
        choices=TwoFactorMethod.choices,
        blank=True,
        default='',
        db_default='',
    )

    # Tell the person when a device or browser they have not used before signs
    # in. Off until they turn it on.
    login_alerts_email = models.BooleanField(default=False, db_default=False)
    login_alerts_sms = models.BooleanField(default=False, db_default=False)

    class Meta:
        constraints = [
            # On without a method is a state with no answer to "where does the
            # code go", which would lock the person out at their next sign-in.
            models.CheckConstraint(
                condition=~models.Q(two_factor_enabled=True, two_factor_method=''),
                name='two_factor_needs_a_method',
            ),
        ]
