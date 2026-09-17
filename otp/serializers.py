"""Serializers for phone verification.

Thin on purpose: the rules live in `otp/services.py` so they stay usable from a
management command or a websocket consumer. What is here is the field
validation worth doing before a single Telnyx call is made - a malformed
number, a country we do not serve, or a number somebody else already owns are
all cheaper to reject now than after we have paid to text it.
"""
from rest_framework import serializers

from otp.services import start_phone_verification, verify_phone
from sms.client import is_allowed_destination
from user.models import Profile, phone_number_validator


class RequestPhoneCodeSerializer(serializers.Serializer):
    """Ask Telnyx to text a code.

    `phone_number` is optional: omitted, the number already on the profile is
    used. Supplying one is how a user verifies a number they have just changed
    to - `Profile.save()` and `SmsOtp.phone_no` are both built for that case.
    """

    phone_number = serializers.CharField(
        max_length=16,
        required=False,
        allow_blank=True,
        help_text='International format, e.g. +639171234567. '
                  'Omit to use the number on your profile.',
    )

    def validate_phone_number(self, value):
        if not value:
            return ''

        value = value.strip()
        # Raises DjangoValidationError, which loopd/exceptions.py translates.
        phone_number_validator(value)

        if not is_allowed_destination(value):
            raise serializers.ValidationError('We cannot send codes to that country.')

        profile = self.context['request'].user.profile
        taken = Profile.objects.filter(phone_number=value).exclude(pk=profile.pk)
        if taken.exists():
            raise serializers.ValidationError('That phone number is already in use.')

        return value

    def save(self, **kwargs):
        # OtpError (cooldown, spam ceiling) and SmsError (Telnyx down) both
        # propagate; loopd/exceptions.py turns them into a 400 in the envelope.
        return start_phone_verification(
            self.context['request'].user,
            self.validated_data.get('phone_number') or None,
        )


class VerifyPhoneSerializer(serializers.Serializer):
    """Confirm the code and flip `Profile.phone_no_verified`."""

    code = serializers.CharField(max_length=10)

    def save(self, **kwargs):
        return verify_phone(self.context['request'].user, self.validated_data['code'])
