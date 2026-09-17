"""Settings → Security."""
from django.conf import settings
from django.db import models

from .base import UserSettings


class SecuritySettings(UserSettings):
    """How an account is protected at sign-in.

    NOTHING READS THESE YET. Sign-in does not ask for a second code, and no
    alert is sent; the web screen shows both as "Not available yet" and saves
    nothing. The API shows all four fields (GET /users/me/settings/security/)
    but saves only the login alerts: two-factor stays read-only there until a
    flow exists that proves where the codes will go.

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

    Each field's comment says what the setting is for, and what changing it
    does TODAY.
    """

    class TwoFactorMethod(models.TextChoices):
        SMS = 'sms', 'Text message'
        EMAIL = 'email', 'Email'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='security_settings',
    )

    # Two-factor authentication - a second step at sign-in.
    #   On  - after the password, sign-in asks for a one-time code sent to the
    #         person (where: `two_factor_method`), so somebody who has learned
    #         the password still cannot get in without their phone or inbox.
    #   Off - the password alone signs in.
    # It cannot be On without a method - the `two_factor_needs_a_method`
    # constraint below refuses it.
    # Today: changing it changes nothing - sign-in never asks for a code. It is
    # read-only in the API and "Not available yet" on the web.
    two_factor_enabled = models.BooleanField(default=False, db_default=False)

    # Where the sign-in code goes when two-factor is on.
    #   (blank) - not set up yet.
    #   sms     - "Text message": a text to the verified phone number.
    #   email   - "Email": an email to the verified address.
    # Only a VERIFIED phone number or email address may be chosen: a code sent
    # to an unproven address is a code sent to whoever typed that address in.
    # Today: changing it changes nothing, and like the switch above it is
    # read-only in the API.
    two_factor_method = models.CharField(
        max_length=5,
        choices=TwoFactorMethod.choices,
        blank=True,
        default='',
        db_default='',
    )

    # Login alerts - a warning when a device or browser the person has not used
    # before signs in to their account, so they can act quickly if it was not
    # them. One switch per channel; both start Off until the person turns them
    # on.
    # Today: changing them changes nothing - no alert is ever sent. The API
    # saves them (PATCH /users/me/settings/security/), but the web marks login
    # alerts "Not available yet" and sends nothing.
    #
    # On: the alert arrives by email, at the account's email address.
    login_alerts_email = models.BooleanField(default=False, db_default=False)
    # On: the alert arrives by text, at the verified phone number.
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
