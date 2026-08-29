"""Tests for code generation and the OTP model rules.

The HTTP-level verification flow is covered in user/tests.py; this file is
about the pieces underneath it.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from otp.models import EmailOtp, OtpError, SmsOtp
from otp.utils import default_expiry, generate_code


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
