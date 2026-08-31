"""Tests for code generation and the OTP model rules.

The HTTP-level verification flow is covered in user/tests.py; this file is
about the pieces underneath it.
"""
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from otp.models import EmailOtp, OtpError, SmsOtp
from otp.utils import default_expiry, generate_code
from user.models import Profile
from utils.sms import DEV_BYPASS_CODE, SmsError


class GenerateCodeTests(TestCase):
    def test_default_length_comes_from_settings(self):
        self.assertEqual(len(generate_code()), settings.OTP_CODE_LENGTH)

    def test_explicit_length_is_honoured(self):
        self.assertEqual(len(generate_code(4)), 4)

    def test_codes_are_all_digits(self):
        for _ in range(200):
            self.assertTrue(generate_code().isdigit())

    def test_leading_zeros_are_preserved(self):
        # Built digit by digit, so every code is exactly OTP_CODE_LENGTH
        # characters - a numeric conversion would silently shorten some.
        self.assertTrue(
            all(len(generate_code()) == settings.OTP_CODE_LENGTH for _ in range(500))
        )

    def test_codes_are_not_obviously_repeating(self):
        # Not a randomness test - just a guard against a stuck generator.
        self.assertGreater(len({generate_code() for _ in range(100)}), 50)


class DefaultExpiryTests(TestCase):
    def test_expiry_is_ttl_in_the_future(self):
        delta = default_expiry() - timezone.now()
        self.assertGreater(delta, settings.OTP_TTL - timedelta(seconds=5))
        self.assertLessEqual(delta, settings.OTP_TTL)


class EmailOtpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='yul', email='yul@example.com', password='x', is_active=False
        )
        self.profile = self.user.profile

    def age_past_cooldown(self, otp):
        EmailOtp.objects.filter(pk=otp.pk).update(
            updated_at=timezone.now() - settings.OTP_RESEND_COOLDOWN - timedelta(seconds=1)
        )

    def test_issue_creates_a_row_with_an_expiry(self):
        otp = EmailOtp.issue(self.profile)
        self.assertEqual(len(otp.code), settings.OTP_CODE_LENGTH)
        self.assertFalse(otp.is_expired)
        self.assertEqual(otp.attempts, 0)

    def test_issue_is_rate_limited(self):
        EmailOtp.issue(self.profile)
        with self.assertRaises(OtpError):
            EmailOtp.issue(self.profile)

    def test_issue_replaces_rather_than_accumulates(self):
        first = EmailOtp.issue(self.profile)
        self.age_past_cooldown(first)
        second = EmailOtp.issue(self.profile)

        self.assertEqual(EmailOtp.objects.filter(profile=self.profile).count(), 1)
        self.assertEqual(first.pk, second.pk)

    def test_verify_accepts_the_right_code_and_consumes_it(self):
        otp = EmailOtp.issue(self.profile)
        self.assertTrue(otp.verify(otp.code))
        self.assertFalse(EmailOtp.objects.filter(profile=self.profile).exists())

    def test_verify_tolerates_surrounding_whitespace(self):
        # People paste codes out of emails.
        otp = EmailOtp.issue(self.profile)
        self.assertTrue(otp.verify(f'  {otp.code} '))

    def test_verify_rejects_and_counts_a_wrong_code(self):
        otp = EmailOtp.issue(self.profile)
        with self.assertRaises(OtpError):
            otp.verify('999999' if otp.code != '999999' else '111111')
        otp.refresh_from_db()
        self.assertEqual(otp.attempts, 1)

    def test_verify_rejects_an_expired_code(self):
        otp = EmailOtp.issue(self.profile)
        EmailOtp.objects.filter(pk=otp.pk).update(
            expiry=timezone.now() - timedelta(seconds=1)
        )
        otp.refresh_from_db()
        with self.assertRaises(OtpError):
            otp.verify(otp.code)

    def test_verify_stops_accepting_once_attempts_run_out(self):
        otp = EmailOtp.issue(self.profile)
        wrong = '999999' if otp.code != '999999' else '111111'
        for _ in range(settings.OTP_MAX_ATTEMPTS):
            with self.assertRaises(OtpError):
                otp.verify(wrong)

        # The correct code no longer helps.
        with self.assertRaises(OtpError):
            otp.verify(otp.code)
        self.assertTrue(EmailOtp.objects.filter(profile=self.profile).exists())

    def test_str_does_not_crash(self):
        otp = EmailOtp.issue(self.profile)
        self.assertIn('yul', str(otp))

    def test_deleting_the_user_deletes_the_code(self):
        EmailOtp.issue(self.profile)
        self.user.delete()
        self.assertFalse(EmailOtp.objects.exists())


class SmsOtpTests(TestCase):
    def test_email_and_sms_codes_coexist_for_one_profile(self):
        user = User.objects.create_user(username='ana', password='x')
        EmailOtp.issue(user.profile)
        SmsOtp.issue(user.profile)

        # Separate one-to-ones, so verifying an email cannot consume the
        # pending SMS code or vice versa.
        self.assertEqual(user.profile.emailotp_profile.profile_id, user.profile.pk)
        self.assertEqual(user.profile.smsotp_profile.profile_id, user.profile.pk)


class PhoneVerificationApiTests(APITestCase):
    """The two endpoints at /api/v1/users/me/phone/.

    Telnyx is unconfigured throughout (no TELNYX_API_KEY), which is the path
    utils.sms takes locally: nothing is sent and DEV_BYPASS_CODE is accepted.
    That keeps these tests offline while still exercising every branch this
    project owns - the ceilings, the cooldown, the attempt count and the
    both-fields-in-one-save rule on Profile.
    """

    def setUp(self):
        # The ceilings live in the cache, which is process-wide and would
        # otherwise leak counts from one test into the next.
        cache.clear()

        self.user = User.objects.create_user(
            'yul', 'yul@example.com', 'correct-horse-battery'
        )
        self.user.profile.phone_number = '+639171234567'
        self.user.profile.save()
        self.client.force_authenticate(self.user)

        self.request_url = reverse('otp:phone-request-code')
        self.verify_url = reverse('otp:phone-verify')

    def request_code(self, **payload):
        return self.client.post(self.request_url, payload, format='json')

    def verify(self, code):
        return self.client.post(self.verify_url, {'code': code}, format='json')

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.request_code().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_request_code_creates_a_pending_row_for_the_profile_number(self):
        self.assertEqual(self.request_code().status_code, status.HTTP_200_OK)

        otp = SmsOtp.objects.get(profile=self.user.profile)
        self.assertEqual(otp.phone_no, '+639171234567')
        # Telnyx holds the code; there is nothing local to store.
        self.assertEqual(otp.code, '')

    def test_request_code_is_rate_limited_by_the_cooldown(self):
        self.assertEqual(self.request_code().status_code, status.HTTP_200_OK)
        self.assertEqual(self.request_code().status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify_marks_the_number_verified(self):
        self.request_code()
        self.assertEqual(self.verify(DEV_BYPASS_CODE).status_code, status.HTTP_200_OK)

        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.phone_no_verified)
        self.assertEqual(self.user.profile.phone_number, '+639171234567')
        # Single use.
        self.assertFalse(SmsOtp.objects.filter(profile=self.user.profile).exists())

    def test_a_wrong_code_is_counted_and_does_not_verify(self):
        self.request_code()
        self.assertEqual(self.verify('999999').status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(SmsOtp.objects.get(profile=self.user.profile).attempts, 1)
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.phone_no_verified)

    def test_attempts_run_out(self):
        self.request_code()
        for _ in range(settings.OTP_MAX_ATTEMPTS):
            self.verify('999999')

        # Even the right code is refused once the budget is spent.
        self.assertEqual(
            self.verify(DEV_BYPASS_CODE).status_code, status.HTTP_400_BAD_REQUEST
        )
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.phone_no_verified)

    def test_verify_without_a_pending_code_is_refused(self):
        self.assertEqual(
            self.verify(DEV_BYPASS_CODE).status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_a_new_number_can_be_verified_and_replaces_the_old_one(self):
        # The change-my-number path. Profile.save() only keeps the verified flag
        # because verify_phone writes the number and the flag in one save.
        self.request_code(phone_number='+639179876543')
        self.assertEqual(self.verify(DEV_BYPASS_CODE).status_code, status.HTTP_200_OK)

        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.phone_number, '+639179876543')
        self.assertTrue(self.user.profile.phone_no_verified)

    def test_a_code_sent_to_one_number_cannot_verify_another(self):
        # Request for a new number, then edit the profile mid-flow. The verify
        # must land on the number the code went to, not the profile's current one.
        self.request_code(phone_number='+639179876543')
        Profile.objects.filter(pk=self.user.profile.pk).update(
            phone_number='+639170000000'
        )

        self.assertEqual(self.verify(DEV_BYPASS_CODE).status_code, status.HTTP_200_OK)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.phone_number, '+639179876543')

    def test_a_number_someone_else_owns_is_refused(self):
        other = User.objects.create_user('maya', 'maya@example.com', 'pw-correct-horse')
        other.profile.phone_number = '+639179876543'
        other.profile.save()

        response = self.request_code(phone_number='+639179876543')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SmsOtp.objects.filter(profile=self.user.profile).exists())

    def test_a_malformed_number_is_refused(self):
        self.assertEqual(
            self.request_code(phone_number='09171234567').status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    @override_settings(SMS_ALLOWED_COUNTRY_PREFIXES=['+63'])
    def test_a_country_we_do_not_serve_is_refused(self):
        # The pumping defence: never pay to text a country we do not operate in.
        self.assertEqual(
            self.request_code(phone_number='+15550001111').status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_a_profile_with_no_number_is_told_to_add_one(self):
        # Set on the instance, not through a queryset: force_authenticate hands
        # the view this very object, so a .update() would leave it holding a
        # stale cached profile.
        self.user.profile.phone_number = None
        self.user.profile.save()
        self.assertEqual(
            self.request_code().status_code, status.HTTP_400_BAD_REQUEST
        )

    @override_settings(OTP_MAX_SENDS_PER_HOUR=2, OTP_RESEND_COOLDOWN=timedelta(seconds=0))
    def test_the_hourly_ceiling_stops_a_slow_drip(self):
        # The cooldown is disabled so the only thing under test is the ceiling.
        self.assertEqual(self.request_code().status_code, status.HTTP_200_OK)
        self.assertEqual(self.request_code().status_code, status.HTTP_200_OK)
        self.assertEqual(self.request_code().status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(OTP_MAX_SENDS_PER_HOUR=1, OTP_RESEND_COOLDOWN=timedelta(seconds=0))
    def test_the_ceiling_also_counts_per_number_across_accounts(self):
        # Harassment rather than fraud: many accounts, one victim's number.
        self.request_code(phone_number='+639179876543')

        other = User.objects.create_user('maya', 'maya@example.com', 'pw-correct-horse')
        self.client.force_authenticate(other)
        self.assertEqual(
            self.request_code(phone_number='+639179876543').status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_a_telnyx_failure_does_not_hold_the_cooldown(self):
        # A provider outage is our fault, not the user's, so the row is dropped
        # and they can retry immediately.
        with mock.patch(
            'otp.services.send_verification_code', side_effect=SmsError('boom')
        ):
            self.assertEqual(
                self.request_code().status_code, status.HTTP_503_SERVICE_UNAVAILABLE
            )

        self.assertFalse(SmsOtp.objects.filter(profile=self.user.profile).exists())
        self.assertEqual(self.request_code().status_code, status.HTTP_200_OK)
