"""Version 1 of the phone-verification API.

    POST /api/v1/users/me/phone/request-code/   text a code to the number
    POST /api/v1/users/me/phone/verify/         confirm it -> phone_no_verified

HOW SMS OTP WORKS HERE
----------------------
These two endpoints drive the Telnyx **Verify** API, not a code this project
generates. Telnyx creates the code, sends it, remembers it, and answers whether
what the user typed matches. Nothing in this repo ever holds an SMS code. The
reasoning, and what that means for `SmsOtp`, is at the top of sms/verification.py.

Both endpoints require a signed-in user - unlike email verification, which has
to work before the account is usable. That is most of the anti-abuse story on
its own; the rest is the cooldown and the hourly/daily ceilings enforced in
otp/services.py.

Nothing here builds a response body by hand: `loopd.renderers` wraps every reply
in the {code, message, data} envelope, so an action returns its payload and
names its `message` in `success_messages`.
"""
from django.conf import settings
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from otp.serializers import RequestPhoneCodeSerializer, VerifyPhoneSerializer
from sms.verification import DEV_BYPASS_CODE
from user.serializers import ProfileSerializer


def verification_payload(otp):
    """What the client needs to drive the "enter your code" screen."""
    payload = {
        'phone_number': otp.phone_no,
        'expires_at': otp.expiry,
        'resend_available_in': otp.seconds_until_resend,
    }

    # With Telnyx unconfigured nothing is actually sent, so development returns
    # the code that sms.verification accepts instead. Guarded on DEBUG as well: in a
    # live deployment this line would hand out a working code.
    if settings.DEBUG and not settings.TELNYX_API_KEY:
        payload['debug_code'] = DEV_BYPASS_CODE

    return payload


class PhoneVerificationViewSet(viewsets.GenericViewSet):
    """Verifying the signed-in user's phone number."""

    permission_classes = [IsAuthenticated]
    serializer_class = RequestPhoneCodeSerializer

    # One serializer per action beats branching inside each method.
    serializer_classes = {
        'request_code': RequestPhoneCodeSerializer,
        'verify': VerifyPhoneSerializer,
    }

    success_messages = {
        'request_code': 'Verification code sent.',
        'verify': 'Phone number verified.',
    }

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.serializer_class)

    @action(
        detail=False,
        methods=['post'],
        url_path='request-code',
        url_name='request-code',
    )
    def request_code(self, request):
        """Have Telnyx text a fresh code.

        Send `phone_number` to verify a number that is not on the profile yet;
        omit it to use the one already there. Refused while the resend cooldown
        is running, or once the hourly or daily ceiling is reached.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        otp = serializer.save()
        return Response(verification_payload(otp))

    @action(detail=False, methods=['post'])
    def verify(self, request):
        """Confirm the code and mark the number verified.

        The code is checked against the number it was actually sent to, not
        whatever is on the profile now, so editing the profile mid-flow cannot
        be used to verify a number the user does not control.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        return Response(
            ProfileSerializer(profile, context=self.get_serializer_context()).data
        )
