"""End-to-end tests for the account and profile API.

Each test asserts the envelope (`code` 1 or -1) as well as the payload,
because the envelope is the contract the client actually codes against.
"""
import io
import shutil
import tempfile
from datetime import timedelta
from unittest import mock
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from otp.models import EmailOtp
from otp.services import verify_email
from user.models import (
    AppearanceSettings,
    MessagingSettings,
    NotificationSettings,
    PrivacySettings,
    Profile,
    SecuritySettings,
)
from user.serializers import SETTINGS_SERIALIZERS
from user.user_settings import SETTINGS_MODELS
from utils.mail import EmailError

MEDIA_ROOT = tempfile.mkdtemp(prefix='loopd-test-media-')


def make_image_upload(name='avatar.png'):
    buffer = io.BytesIO()
    Image.new('RGB', (12, 12), color='blue').save(buffer, format='PNG')
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type='image/png')


class EnvelopeAssertions:
    """Shared assertions so each test reads as one line of intent."""

    def assertSuccess(self, response, expected_status=status.HTTP_200_OK):
        body = response.json()
        self.assertEqual(response.status_code, expected_status, body)
        self.assertEqual(body['code'], 1, body)
        self.assertIn('message', body)
        return body['data']

    def assertFailure(self, response, expected_status=status.HTTP_400_BAD_REQUEST):
        body = response.json()
        self.assertEqual(response.status_code, expected_status, body)
        self.assertEqual(body['code'], -1, body)
        self.assertTrue(body['message'], body)
        return body


@override_settings(REQUIRE_EMAIL_VERIFICATION=False)
class RegisterTests(EnvelopeAssertions, APITestCase):
    """Registration with verification switched OFF.

    No longer the default - see RegisterWithVerificationTests for that - but
    still supported, and the mode a local checkout without a Resend key wants.
    """

    def setUp(self):
        # The per-address send ceiling lives in the cache, which is
        # process-wide and would otherwise leak counts from one test into the
        # next - every test here registers the same address.
        cache.clear()
        self.url = reverse('user:user-register')
        self.payload = {
            'username': 'yul',
            'email': 'yul@example.com',
            'password': 'a-strong-passphrase-42',
            'confirm_password': 'a-strong-passphrase-42',
            'first_name': 'Yul',
            'display_name': 'Yul G',
            'phone_number': '+639171234567',
        }

    def test_creates_an_active_account_without_signing_in(self):
        data = self.assertSuccess(
            self.client.post(self.url, self.payload), status.HTTP_201_CREATED
        )

        self.assertEqual(data['user']['username'], 'yul')
        # Registering never hands out credentials - that is login's job.
        self.assertNotIn('tokens', data)
        # Verification is off, so there is no code to enter either.
        self.assertNotIn('verification', data)

        user = User.objects.get(username='yul')
        self.assertTrue(user.is_active)
        # Nobody is ever asked for a code in this mode, so the account must be
        # marked verified here - leaving it False would strand it as
        # permanently unverified and hand a later ban a way out.
        self.assertTrue(user.profile.email_verified)
        self.assertFalse(EmailOtp.objects.filter(profile=user.profile).exists())

    def test_the_new_account_can_log_in_immediately(self):
        self.client.post(self.url, self.payload)
        data = self.assertSuccess(
            self.client.post(
                reverse('user:user-login'),
                {'username': 'yul', 'password': self.payload['password']},
            )
        )
        self.assertIn('access', data['tokens'])

    def test_the_token_from_logging_in_works_straight_away(self):
        self.client.post(self.url, self.payload)
        tokens = self.assertSuccess(
            self.client.post(
                reverse('user:user-login'),
                {'username': 'yul', 'password': self.payload['password']},
            )
        )['tokens']

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}')
        me = self.assertSuccess(self.client.get(reverse('user:user-me')))
        self.assertEqual(me['username'], 'yul')

    def test_profile_is_filled_in_at_signup(self):
        self.assertSuccess(
            self.client.post(self.url, self.payload), status.HTTP_201_CREATED
        )
        user = User.objects.get(username='yul')
        # The password must be hashed, never stored as sent.
        self.assertNotEqual(user.password, self.payload['password'])
        self.assertTrue(user.check_password(self.payload['password']))

        self.assertEqual(user.profile.display_name, 'Yul G')
        self.assertEqual(user.profile.phone_number, '+639171234567')

    def test_password_is_never_echoed_back(self):
        data = self.assertSuccess(
            self.client.post(self.url, self.payload), status.HTTP_201_CREATED
        )
        self.assertNotIn('password', data['user'])

    def test_rejects_mismatched_confirmation(self):
        self.payload['confirm_password'] = 'something-else-entirely'
        body = self.assertFailure(self.client.post(self.url, self.payload))
        self.assertIn('confirm_password', body['data'])

    def test_rejects_weak_password(self):
        self.payload['password'] = self.payload['confirm_password'] = '123'
        body = self.assertFailure(self.client.post(self.url, self.payload))
        self.assertIn('password', body['data'])

    def test_rejects_duplicate_username_regardless_of_case(self):
        self.client.post(self.url, self.payload)
        self.payload.update(username='YUL', email='other@example.com', phone_number='')
        body = self.assertFailure(self.client.post(self.url, self.payload))
        self.assertIn('username', body['data'])

    def test_rejects_duplicate_email_regardless_of_case(self):
        self.client.post(self.url, self.payload)
        self.payload.update(username='other', email='YUL@example.com', phone_number='')
        body = self.assertFailure(self.client.post(self.url, self.payload))
        self.assertIn('email', body['data'])

    def test_rejects_duplicate_phone_number(self):
        self.client.post(self.url, self.payload)
        self.payload.update(username='other', email='other@example.com')
        body = self.assertFailure(self.client.post(self.url, self.payload))
        self.assertIn('phone_number', body['data'])

    def test_rejects_badly_formatted_phone_number(self):
        self.payload['phone_number'] = '0917 123 4567'
        body = self.assertFailure(self.client.post(self.url, self.payload))
        self.assertIn('phone_number', body['data'])

    def test_phone_number_is_optional(self):
        self.payload.pop('phone_number')
        self.assertSuccess(
            self.client.post(self.url, self.payload), status.HTTP_201_CREATED
        )
        self.assertIsNone(User.objects.get(username='yul').profile.phone_number)

    def test_blank_phone_numbers_do_not_collide(self):
        # Two users with no number must both be allowed under `unique`.
        self.payload['phone_number'] = ''
        self.assertSuccess(
            self.client.post(self.url, self.payload), status.HTTP_201_CREATED
        )
        self.payload.update(username='second', email='second@example.com')
        self.assertSuccess(
            self.client.post(self.url, self.payload), status.HTTP_201_CREATED
        )


@override_settings(GOOGLE_CLIENT_ID='google-client-id')
class GoogleAuthTests(EnvelopeAssertions, APITestCase):
    def setUp(self):
        self.url = reverse('user:user-google')
        self.claims = {
            'sub': 'google-subject-123',
            'email': 'yul@example.com',
            'email_verified': True,
            'given_name': 'Yul',
            'family_name': 'G',
            'name': 'Yul G',
        }

    @patch('user.serializers.id_token.verify_oauth2_token')
    def test_creates_an_account_and_returns_tokens(self, verify_token):
        verify_token.return_value = self.claims
        data = self.assertSuccess(
            self.client.post(self.url, {'credential': 'google-id-token'}),
            status.HTTP_201_CREATED,
        )

        self.assertEqual(data['user']['email'], 'yul@example.com')
        self.assertEqual(data['user']['profile']['display_name'], 'Yul G')
        self.assertIn('access', data['tokens'])
        self.assertIn('refresh', data['tokens'])
        verify_token.assert_called_once()

    @patch('user.serializers.id_token.verify_oauth2_token')
    def test_logs_in_to_the_existing_email_account(self, verify_token):
        user = User.objects.create_user('existing', 'yul@example.com', 'password')
        verify_token.return_value = self.claims

        data = self.assertSuccess(self.client.post(self.url, {'credential': 'google-id-token'}))

        self.assertEqual(data['user']['id'], user.id)
        self.assertEqual(User.objects.filter(email__iexact='yul@example.com').count(), 1)

    @patch('user.serializers.id_token.verify_oauth2_token')
    def test_rejects_an_unverified_google_email(self, verify_token):
        verify_token.return_value = {**self.claims, 'email_verified': False}

        body = self.assertFailure(self.client.post(self.url, {'credential': 'google-id-token'}))

        self.assertIn('credential', body['data'])

    @patch('user.serializers.id_token.verify_oauth2_token')
    def test_a_google_account_is_marked_email_verified(self, verify_token):
        # GoogleCredentialSerializer refuses any token whose email_verified
        # claim is not True, so arriving here means Google vouched for the
        # address. Leaving the flag False would make the account permanently
        # unverified: unable to use password login later, and a standing
        # target for verification codes it can never need.
        verify_token.return_value = self.claims
        self.assertSuccess(
            self.client.post(self.url, {'credential': 'x'}), status.HTTP_201_CREATED
        )

        user = User.objects.get(email='yul@example.com')
        self.assertTrue(user.profile.email_verified)

    @patch('user.serializers.id_token.verify_oauth2_token')
    def test_signing_in_with_google_verifies_an_existing_unverified_account(
        self, verify_token
    ):
        # Registered with a password, never opened the email, then used the
        # Google button. Google has now proven the address, so the password
        # login they abandoned must start working.
        user = User.objects.create_user(
            'yul', 'yul@example.com', 'a-strong-passphrase-42'
        )
        self.assertFalse(user.profile.email_verified)

        verify_token.return_value = self.claims
        self.assertSuccess(self.client.post(self.url, {'credential': 'x'}))

        user.profile.refresh_from_db()
        self.assertTrue(user.profile.email_verified)

        with override_settings(REQUIRE_EMAIL_VERIFICATION=True):
            self.assertSuccess(
                self.client.post(
                    reverse('user:user-login'),
                    {'username': 'yul', 'password': 'a-strong-passphrase-42'},
                )
            )

@override_settings(REQUIRE_EMAIL_VERIFICATION=True)
class RegisterWithVerificationTests(EnvelopeAssertions, APITestCase):
    """Registration as it now behaves by default: inactive until verified.

    The override is redundant with the current setting and kept deliberately,
    so that flipping REQUIRE_EMAIL_VERIFICATION does not silently stop testing
    this path.
    """

    def setUp(self):
        # The per-address send ceiling lives in the cache, which is
        # process-wide; every test here registers the same address, so without
        # this the sixth one is refused by a limit the test never mentions.
        cache.clear()
        self.url = reverse('user:user-register')
        self.payload = {
            'username': 'yul',
            'email': 'yul@example.com',
            'password': 'a-strong-passphrase-42',
            'confirm_password': 'a-strong-passphrase-42',
        }

    def test_creates_an_unverified_account_and_issues_a_code(self):
        data = self.assertSuccess(
            self.client.post(self.url, self.payload), status.HTTP_201_CREATED
        )
        self.assertNotIn('tokens', data)
        self.assertEqual(data['verification']['email'], 'yul@example.com')

        user = User.objects.get(username='yul')
        # Unverified, but NOT banned: is_active is an admin's field and
        # registration has no business writing it.
        self.assertFalse(user.profile.email_verified)
        self.assertTrue(user.is_active)

        otp = EmailOtp.objects.get(profile=user.profile)
        self.assertEqual(len(otp.code), 6)
        self.assertTrue(otp.code.isdigit())

    def test_the_code_is_not_leaked_outside_debug(self):
        # DEBUG is False under test, which is what production looks like.
        data = self.assertSuccess(
            self.client.post(self.url, self.payload), status.HTTP_201_CREATED
        )
        self.assertNotIn('debug_code', data['verification'])

    def test_the_new_account_cannot_log_in_yet(self):
        self.client.post(self.url, self.payload)
        body = self.assertFailure(
            self.client.post(
                reverse('user:user-login'),
                {'username': 'yul', 'password': self.payload['password']},
            ),
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(body['message'], 'Verify your email address before signing in.')

    def test_the_default_settings_require_verification(self):
        # The override on this class matches the shipped default; if someone
        # flips REQUIRE_EMAIL_VERIFICATION this is the test that objects.
        self.assertTrue(settings.REQUIRE_EMAIL_VERIFICATION)

    def test_the_new_account_cannot_log_in_until_it_is_verified(self):
        self.client.post(self.url, self.payload)

        body = self.assertFailure(
            self.client.post(
                reverse('user:user-login'),
                {'username': 'yul', 'password': self.payload['password']},
            ),
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            body['message'], 'Verify your email address before signing in.'
        )

    @override_settings(RESEND_API_KEY='re_test')
    def test_the_whole_flow_end_to_end(self):
        # 1. register -> 2. code emailed -> 3. verify -> 4. account verified.
        with mock.patch('utils.mail._post', return_value={'id': 'abc'}) as post:
            self.assertSuccess(
                self.client.post(self.url, self.payload), status.HTTP_201_CREATED
            )

        user = User.objects.get(username='yul')
        self.assertFalse(user.profile.email_verified)

        # The code really did go out, and it is the one on the row.
        otp = EmailOtp.objects.get(profile=user.profile)
        self.assertIn(otp.code, post.call_args.args[1]['text'])
        self.assertEqual(post.call_args.args[1]['to'], ['yul@example.com'])

        data = self.assertSuccess(
            self.client.post(
                reverse('user:user-verify-email'),
                {'email': 'yul@example.com', 'code': otp.code},
            )
        )

        user.profile.refresh_from_db()
        self.assertTrue(user.profile.email_verified)
        # Verifying signs them in, so there is no second round trip.
        self.assertIn('access', data['tokens'])
        # Single-use: the code is gone.
        self.assertFalse(EmailOtp.objects.filter(profile=user.profile).exists())

    @override_settings(RESEND_API_KEY='re_test')
    def test_a_delivery_failure_still_creates_a_recoverable_account(self):
        # Failing the request outright would strand the user: registering
        # again answers "that email already exists", and a client shown an
        # error has no reason to call resend-code.
        with mock.patch('utils.mail._post', side_effect=EmailError('boom')):
            data = self.assertSuccess(
                self.client.post(self.url, self.payload), status.HTTP_201_CREATED
            )

        self.assertIsNone(data['verification'])

        user = User.objects.get(username='yul')
        self.assertFalse(user.profile.email_verified)
        # No row, so the resend cooldown is not burned by our outage.
        self.assertFalse(EmailOtp.objects.filter(profile=user.profile).exists())

        # And the documented recovery actually works.
        with mock.patch('utils.mail._post', return_value={'id': 'abc'}):
            self.assertSuccess(
                self.client.post(
                    reverse('user:user-resend-code'), {'email': 'yul@example.com'}
                )
            )
        self.assertTrue(EmailOtp.objects.filter(profile=user.profile).exists())

@override_settings(REQUIRE_EMAIL_VERIFICATION=True)
class DeactivatedAccountTests(EnvelopeAssertions, APITestCase):
    """is_active means "not banned"; Profile.email_verified means "confirmed".

    Keeping them apart is the whole point of the second flag. Collapsed into
    one, a deactivated account is indistinguishable from a never-verified one,
    and the two public verification endpoints become a way back in.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='yul', email='yul@example.com', password='a-strong-passphrase-42'
        )
        # A verified, long-standing account - then an admin deactivates it.
        self.user.profile.email_verified = True
        self.user.profile.save(update_fields=['email_verified'])
        User.objects.filter(pk=self.user.pk).update(is_active=False)

    def test_a_deactivated_account_cannot_request_a_code(self):
        body = self.assertFailure(
            self.client.post(
                reverse('user:user-resend-code'), {'email': 'yul@example.com'}
            )
        )
        self.assertIn('email', body['data'])
        self.assertFalse(EmailOtp.objects.filter(profile=self.user.profile).exists())

    def test_a_deactivated_account_cannot_verify_its_way_back_in(self):
        # Even handed a live code - say one issued before the ban - the
        # endpoint refuses, so the ban cannot be undone from outside.
        EmailOtp.issue(self.user.profile)
        otp = EmailOtp.objects.get(profile=self.user.profile)

        self.assertFailure(
            self.client.post(
                reverse('user:user-verify-email'),
                {'email': 'yul@example.com', 'code': otp.code},
            )
        )

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_verifying_never_writes_is_active(self):
        # The root cause of the un-ban path was verify_email writing is_active.
        # It must only ever write the profile flag.
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        self.user.profile.email_verified = False
        self.user.profile.save(update_fields=['email_verified'])
        otp = EmailOtp.issue(self.user.profile)

        verify_email(self.user, otp.code)

        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.email_verified)
        # Unchanged by verification, in either direction.
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        otp = EmailOtp.issue(self.user.profile)
        self.user.profile.email_verified = False
        self.user.profile.save(update_fields=['email_verified'])
        verify_email(User.objects.get(pk=self.user.pk), otp.code)
        self.assertFalse(User.objects.get(pk=self.user.pk).is_active)

    def test_an_unverified_but_unbanned_account_is_still_allowed_through(self):
        # The guard must catch bans without also blocking the ordinary case:
        # active, not yet verified.
        self.user.profile.email_verified = False
        self.user.profile.save(update_fields=['email_verified'])
        User.objects.filter(pk=self.user.pk).update(is_active=True)

        self.assertSuccess(
            self.client.post(
                reverse('user:user-resend-code'), {'email': 'yul@example.com'}
            )
        )

    def test_a_ban_cannot_be_armed_by_changing_the_email_first(self):
        # The whole chain, in order: a user changes their address (which
        # clears email_verified while the account is still active), is banned
        # afterwards, and then tries to verify their way back in. Every step
        # is an ordinary endpoint, which is what made this reachable.
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        self.client.force_authenticate(self.user)
        self.assertSuccess(
            self.client.patch(reverse('user:user-me'), {'email': 'new@example.com'})
        )
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.email_verified)

        # Now the ban lands, on an account sitting in the unverified state.
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.client.force_authenticate(None)

        # Barrier one: the code is never issued.
        self.assertFailure(
            self.client.post(
                reverse('user:user-resend-code'), {'email': 'new@example.com'}
            )
        )

        # Barrier two: even holding a live code, verifying cannot un-ban.
        # EmailOtp.issue bypasses the endpoint entirely, which is the
        # strongest form of the attack.
        EmailOtp.issue(self.user.profile)
        otp = EmailOtp.objects.get(profile=self.user.profile)
        self.assertFailure(
            self.client.post(
                reverse('user:user-verify-email'),
                {'email': 'new@example.com', 'code': otp.code},
            )
        )

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)


class EmailChangeTests(EnvelopeAssertions, APITestCase):
    """Changing the address has to drop the badge that vouched for the old one."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='yul', email='yul@example.com', password='a-strong-passphrase-42'
        )
        self.user.profile.email_verified = True
        self.user.profile.save(update_fields=['email_verified'])
        self.client.force_authenticate(self.user)

    def test_changing_the_email_clears_the_verified_flag(self):
        self.assertSuccess(
            self.client.patch(reverse('user:user-me'), {'email': 'other@example.com'})
        )
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.email_verified)

    def test_rewriting_the_same_email_keeps_the_flag(self):
        # A PATCH that changes a different field must not cost the badge.
        self.assertSuccess(
            self.client.patch(
                reverse('user:user-me'),
                {'email': 'YUL@example.com', 'first_name': 'Yul'},
            )
        )
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.email_verified)

    def test_the_flag_cannot_be_set_through_the_profile_endpoint(self):
        self.user.profile.email_verified = False
        self.user.profile.save(update_fields=['email_verified'])

        self.assertSuccess(
            self.client.patch(
                reverse('user:user-me-profile'), {'email_verified': True}
            )
        )
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.email_verified)

class LoginTests(EnvelopeAssertions, APITestCase):
    def setUp(self):
        self.url = reverse('user:user-login')
        self.password = 'a-strong-passphrase-42'
        self.user = User.objects.create_user(
            username='yul', email='yul@example.com', password=self.password
        )
        # create_user bypasses RegisterSerializer, so the flag has to be set
        # by hand; without it login is refused as unverified.
        self.user.profile.email_verified = True
        self.user.profile.save(update_fields=['email_verified'])

    @override_settings(REQUIRE_EMAIL_VERIFICATION=True)
    def test_unverified_account_is_told_to_verify(self):
        # Unverified, not banned - the account is active throughout.
        self.user.profile.email_verified = False
        self.user.profile.save(update_fields=['email_verified'])
        body = self.assertFailure(
            self.client.post(
                self.url, {'username': 'yul', 'password': self.password}
            ),
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(body['message'], 'Verify your email address before signing in.')

    @override_settings(REQUIRE_EMAIL_VERIFICATION=True)
    def test_a_banned_account_is_not_told_it_is_banned(self):
        # A ban must be indistinguishable from a wrong password: saying so
        # would confirm the account exists, and telling a banned user to
        # "verify your email" sends them to an endpoint that refuses them.
        self.user.profile.email_verified = True
        self.user.profile.save(update_fields=['email_verified'])
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        body = self.assertFailure(
            self.client.post(
                self.url, {'username': 'yul', 'password': self.password}
            ),
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(body['message'], 'Incorrect username or password.')

    def test_unverified_account_with_a_wrong_password_reveals_nothing(self):
        # The "verify your email" hint must only reach someone who already
        # knows the password, or it becomes an account-enumeration oracle.
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        body = self.assertFailure(
            self.client.post(self.url, {'username': 'yul', 'password': 'wrong'}),
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(body['message'], 'Incorrect username or password.')

    def test_login_with_username(self):
        data = self.assertSuccess(
            self.client.post(self.url, {'username': 'yul', 'password': self.password})
        )
        self.assertIn('access', data['tokens'])
        self.assertEqual(data['user']['id'], self.user.id)

    def test_login_with_email_in_the_username_field(self):
        data = self.assertSuccess(
            self.client.post(
                self.url, {'username': 'YUL@example.com', 'password': self.password}
            )
        )
        self.assertEqual(data['user']['username'], 'yul')

    def test_wrong_password_is_rejected(self):
        body = self.assertFailure(
            self.client.post(self.url, {'username': 'yul', 'password': 'nope'}),
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(body['message'], 'Incorrect username or password.')

    def test_unknown_user_gives_the_same_message(self):
        # Identical wording, so the endpoint cannot enumerate accounts.
        body = self.assertFailure(
            self.client.post(self.url, {'username': 'ghost', 'password': 'nope'}),
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(body['message'], 'Incorrect username or password.')


@override_settings(REQUIRE_EMAIL_VERIFICATION=True)
class EmailVerificationTests(EnvelopeAssertions, APITestCase):
    def setUp(self):
        # See the note in RegisterWithVerificationTests.setUp.
        cache.clear()
        self.password = 'a-strong-passphrase-42'
        self.client.post(
            reverse('user:user-register'),
            {
                'username': 'yul',
                'email': 'yul@example.com',
                'password': self.password,
                'confirm_password': self.password,
            },
        )
        self.user = User.objects.get(username='yul')
        self.otp = EmailOtp.objects.get(profile=self.user.profile)

    def verify(self, code, email='yul@example.com'):
        return self.client.post(
            reverse('user:user-verify-email'), {'email': email, 'code': code}
        )

    def test_correct_code_activates_the_account_and_signs_in(self):
        data = self.assertSuccess(self.verify(self.otp.code))

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertIn('access', data['tokens'])

        # Single use: the row is gone, so the code cannot be replayed.
        self.assertFalse(EmailOtp.objects.filter(profile=self.user.profile).exists())

    def test_login_works_after_verification(self):
        self.verify(self.otp.code)
        self.assertSuccess(
            self.client.post(
                reverse('user:user-login'),
                {'username': 'yul', 'password': self.password},
            )
        )

    def test_wrong_code_is_counted(self):
        body = self.assertFailure(self.verify('000000' if self.otp.code != '000000' else '111111'))
        self.assertIn('attempts left', body['message'])

        self.otp.refresh_from_db()
        self.assertEqual(self.otp.attempts, 1)

        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.email_verified)

    def test_code_dies_after_too_many_wrong_attempts(self):
        wrong = '000000' if self.otp.code != '000000' else '111111'
        for _ in range(settings.OTP_MAX_ATTEMPTS):
            self.verify(wrong)

        # Even the right code is refused once the budget is spent.
        body = self.assertFailure(self.verify(self.otp.code))
        self.assertIn('Too many incorrect attempts', body['message'])

        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.email_verified)

    def test_expired_code_is_refused(self):
        EmailOtp.objects.filter(pk=self.otp.pk).update(
            expiry=timezone.now() - timedelta(seconds=1)
        )
        body = self.assertFailure(self.verify(self.otp.code))
        self.assertIn('expired', body['message'])

    def test_unknown_email_is_refused(self):
        self.assertFailure(self.verify(self.otp.code, email='nobody@example.com'))

    def test_already_verified_account_is_refused(self):
        self.verify(self.otp.code)
        body = self.assertFailure(self.verify('123456'))
        self.assertIn('already verified', body['message'])

    def test_resend_is_rate_limited(self):
        body = self.assertFailure(
            self.client.post(
                reverse('user:user-resend-code'), {'email': 'yul@example.com'}
            )
        )
        self.assertIn('Please wait', body['message'])

    def test_resend_replaces_the_code_and_resets_attempts(self):
        wrong = '000000' if self.otp.code != '000000' else '111111'
        self.verify(wrong)

        # Step past the cooldown without waiting for it.
        EmailOtp.objects.filter(pk=self.otp.pk).update(
            updated_at=timezone.now() - settings.OTP_RESEND_COOLDOWN - timedelta(seconds=1)
        )
        self.assertSuccess(
            self.client.post(
                reverse('user:user-resend-code'), {'email': 'yul@example.com'}
            )
        )

        replacement = EmailOtp.objects.get(profile=self.user.profile)
        self.assertEqual(replacement.attempts, 0)
        # The superseded code must stop working.
        self.assertFailure(self.verify(self.otp.code))
        self.assertSuccess(self.verify(replacement.code))

    def test_only_one_code_exists_per_user(self):
        EmailOtp.objects.filter(pk=self.otp.pk).update(
            updated_at=timezone.now() - settings.OTP_RESEND_COOLDOWN - timedelta(seconds=1)
        )
        self.client.post(reverse('user:user-resend-code'), {'email': 'yul@example.com'})
        self.assertEqual(EmailOtp.objects.filter(profile=self.user.profile).count(), 1)


class AuthenticatedFlowTests(EnvelopeAssertions, APITestCase):
    def setUp(self):
        self.password = 'a-strong-passphrase-42'
        self.user = User.objects.create_user(
            username='yul', email='yul@example.com', password=self.password
        )
        # create_user bypasses RegisterSerializer, so the flag has to be set
        # by hand; without it login is refused as unverified.
        self.user.profile.email_verified = True
        self.user.profile.save(update_fields=['email_verified'])
        tokens = self.client.post(
            reverse('user:user-login'), {'username': 'yul', 'password': self.password}
        ).json()['data']['tokens']
        self.refresh = tokens['refresh']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}')

    def test_me_requires_authentication(self):
        self.client.credentials()
        self.assertFailure(
            self.client.get(reverse('user:user-me')), status.HTTP_401_UNAUTHORIZED
        )

    def test_me_returns_the_caller_with_nested_profile(self):
        data = self.assertSuccess(self.client.get(reverse('user:user-me')))
        self.assertEqual(data['username'], 'yul')
        # Nothing was set, so display_label falls back to the username.
        self.assertEqual(data['profile']['display_label'], 'yul')
        # The web app reads the group-email switch from here (frontend-web
        # lib/auth/client.ts), though it no longer lives on Profile itself.
        self.assertTrue(data['profile']['email_enabled'])

    def test_me_updates_account_fields(self):
        data = self.assertSuccess(
            self.client.patch(reverse('user:user-me'), {'first_name': 'Yul'})
        )
        self.assertEqual(data['first_name'], 'Yul')

    def test_me_cannot_take_another_users_email(self):
        User.objects.create_user(username='taken', email='taken@example.com')
        body = self.assertFailure(
            self.client.patch(reverse('user:user-me'), {'email': 'taken@example.com'})
        )
        self.assertIn('email', body['data'])

    def test_me_keeps_its_own_email_on_update(self):
        # Re-sending the value already stored must not trip the unique check.
        self.assertSuccess(
            self.client.patch(reverse('user:user-me'), {'email': 'yul@example.com'})
        )

    def test_username_is_read_only(self):
        self.assertSuccess(self.client.patch(reverse('user:user-me'), {'username': 'hacked'}))
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'yul')

    def test_profile_update(self):
        data = self.assertSuccess(
            self.client.patch(
                reverse('user:user-me-profile'),
                {'display_name': 'Yul G', 'bio': 'hello', 'phone_number': '+639171234567'},
            )
        )
        self.assertEqual(data['display_name'], 'Yul G')
        self.assertEqual(data['phone_number'], '+639171234567')

    def test_phone_verification_flag_cannot_be_written(self):
        self.assertSuccess(
            self.client.patch(reverse('user:user-me-profile'), {'phone_no_verified': True})
        )
        self.assertFalse(Profile.objects.get(user=self.user).phone_no_verified)

    def test_changing_the_number_drops_the_verified_flag(self):
        profile = self.user.profile
        profile.phone_number = '+639171234567'
        profile.phone_no_verified = True
        profile.save()

        data = self.assertSuccess(
            self.client.patch(
                reverse('user:user-me-profile'), {'phone_number': '+639179999999'}
            )
        )
        self.assertFalse(data['phone_no_verified'])

    def test_verified_flag_survives_an_unrelated_edit(self):
        profile = self.user.profile
        profile.phone_number = '+639171234567'
        profile.phone_no_verified = True
        profile.save()

        data = self.assertSuccess(
            self.client.patch(reverse('user:user-me-profile'), {'bio': 'hello'})
        )
        self.assertTrue(data['phone_no_verified'])

    def test_presence_fields_cannot_be_written(self):
        self.assertSuccess(
            self.client.patch(reverse('user:user-me-profile'), {'is_online': True})
        )
        self.assertFalse(Profile.objects.get(user=self.user).is_online)

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_avatar_upload_returns_a_url(self):
        data = self.assertSuccess(
            self.client.patch(
                reverse('user:user-me-profile'),
                {'avatar': make_image_upload()},
                format='multipart',
            )
        )
        self.assertTrue(data['avatar_url'].endswith('.png'))
        self.assertIn(f'avatars/{self.user.id}/', data['avatar_url'])
        # Write-only: the raw field must not come back.
        self.assertNotIn('avatar', data)

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_non_image_upload_is_rejected(self):
        upload = SimpleUploadedFile('x.png', b'not an image', content_type='image/png')
        body = self.assertFailure(
            self.client.patch(
                reverse('user:user-me-profile'), {'avatar': upload}, format='multipart'
            )
        )
        self.assertIn('avatar', body['data'])

    def test_refresh_returns_a_new_pair(self):
        data = self.assertSuccess(
            self.client.post(reverse('user:user-token-refresh'), {'refresh': self.refresh})
        )
        self.assertIn('access', data['tokens'])
        # Rotation is on, so a fresh refresh token comes back too.
        self.assertIn('refresh', data['tokens'])

    def test_logout_blacklists_the_refresh_token(self):
        self.assertSuccess(
            self.client.post(reverse('user:user-logout'), {'refresh': self.refresh})
        )
        self.assertFailure(
            self.client.post(reverse('user:user-token-refresh'), {'refresh': self.refresh}),
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_logout_rejects_a_junk_token(self):
        self.assertFailure(self.client.post(reverse('user:user-logout'), {'refresh': 'junk'}))

    def test_public_detail_hides_contact_details(self):
        other = User.objects.create_user(username='ana', email='ana@example.com')
        other.profile.phone_number = '+639170000000'
        other.profile.bio = 'hi'
        other.profile.save()

        data = self.assertSuccess(
            self.client.get(reverse('user:user-detail', args=[other.id]))
        )
        self.assertEqual(data['username'], 'ana')
        self.assertEqual(data['profile']['bio'], 'hi')
        self.assertNotIn('phone_number', data['profile'])
        self.assertNotIn('date_of_birth', data['profile'])
        # Declared on ProfileSerializer, which this one inherits from - and
        # still nobody's business but the account's own.
        self.assertNotIn('email_enabled', data['profile'])
        self.assertNotIn('email', data)

    def test_missing_user_is_a_clean_envelope_not_an_html_404(self):
        body = self.assertFailure(
            self.client.get(reverse('user:user-detail', args=[99999])), status.HTTP_404_NOT_FOUND
        )
        self.assertIsNone(body['data'])


class UserSearchTests(EnvelopeAssertions, APITestCase):
    """Finding people to start a conversation with."""

    def setUp(self):
        self.me = User.objects.create_user(username='alice', password='x')
        self.me.profile.phone_number = '+639170000001'
        self.me.profile.save()

        self.bob = User.objects.create_user(username='bobby', password='x')
        # Verified in one save, which Profile.save() reads as the verification
        # flow. Search deliberately ignores unverified numbers, so a fixture
        # that skipped this would make the lookup test fail for the wrong reason.
        self.bob.profile.phone_number = '+639170000002'
        self.bob.profile.phone_no_verified = True
        self.bob.profile.save()

        self.client.force_authenticate(self.me)
        self.url = reverse('user:user-search')

    def search(self, term):
        return self.assertSuccess(self.client.get(self.url, {'q': term}))

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertFailure(
            self.client.get(self.url, {'q': 'bob'}), status.HTTP_401_UNAUTHORIZED
        )

    def test_finds_by_username_prefix(self):
        data = self.search('bob')
        self.assertEqual([row['username'] for row in data['results']], ['bobby'])

    def test_finds_by_exact_phone_number(self):
        data = self.search('+639170000002')
        self.assertEqual([row['username'] for row in data['results']], ['bobby'])

    def test_an_unverified_number_finds_nobody(self):
        # Otherwise search routes around find_user: it hands back the squatter,
        # the client shows them, the user taps, and the conversation is then
        # started by USERNAME - which never re-checks the number at all.
        eve = User.objects.create_user(username='eve', password='x')
        eve.profile.phone_number = '+639179999999'
        eve.profile.save()

        self.assertEqual(self.search('+639179999999')['results'], [])

    def test_a_partial_phone_number_finds_nobody(self):
        # Otherwise the endpoint is a way to walk the phone directory.
        data = self.search('+63917000000')
        self.assertEqual(data['results'], [])

    def test_excludes_me(self):
        data = self.search('ali')
        self.assertEqual(data['results'], [])

    def test_excludes_unverified_accounts(self):
        User.objects.filter(pk=self.bob.pk).update(is_active=False)
        self.assertEqual(self.search('bob')['results'], [])

    def test_a_one_character_query_finds_nobody(self):
        self.assertEqual(self.search('b')['results'], [])

    def test_results_do_not_leak_contact_details(self):
        row = self.search('bob')['results'][0]
        self.assertNotIn('email', row)
        self.assertNotIn('phone_number', row['profile'])


class ProfileModelTests(APITestCase):
    def test_profile_is_created_for_every_new_user(self):
        user = User.objects.create_user(username='ana', password='x')
        self.assertTrue(Profile.objects.filter(user=user).exists())

    def test_profile_is_created_for_superusers_too(self):
        user = User.objects.create_superuser(username='root', email='r@x.com', password='x')
        self.assertIsNotNone(user.profile)

    def test_clearing_the_number_drops_the_verified_flag(self):
        user = User.objects.create_user(username='ana', password='x')
        user.profile.phone_number = '+639171234567'
        user.profile.phone_no_verified = True
        user.profile.save()

        user.profile.phone_number = ''
        user.profile.save(update_fields=['phone_number'])
        user.profile.refresh_from_db()
        self.assertFalse(user.profile.phone_no_verified)

    def test_blank_phone_number_is_normalised_to_null(self):
        user = User.objects.create_user(username='ana', password='x')
        user.profile.phone_number = ''
        user.profile.save()
        user.profile.refresh_from_db()
        self.assertIsNone(user.profile.phone_number)

    def test_display_label_prefers_display_name_then_full_name(self):
        user = User.objects.create_user(username='ana', password='x', first_name='Ana', last_name='R')
        self.assertEqual(user.profile.display_label, 'Ana R')
        user.profile.display_name = 'Ana!'
        self.assertEqual(user.profile.display_label, 'Ana!')

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)


class FindUserByPhoneNumberTests(APITestCase):
    """Looking somebody up by phone number requires that they proved it.

    `Profile.phone_number` is unique but its uniqueness says nothing about
    ownership: `phone_no_verified` is a separate flag, so anybody can type a
    stranger's number into their own profile and sit on it. Before this rule,
    that squatted row was the one a lookup returned - which is how a squatter
    ends up added to a private group meant for the number's real owner, and,
    once groups are also delivered over SMS, texted its contents.
    """

    def setUp(self):
        self.number = '+639171234567'
        self.squatter = User.objects.create_user(username='squatter', password='x')
        # Exactly what the profile endpoint allows: set a number, prove nothing.
        self.squatter.profile.phone_number = self.number
        self.squatter.profile.save()

    def test_an_unverified_number_resolves_to_nobody(self):
        from user.lookups import find_user

        self.assertIsNone(find_user(self.number))

    def test_a_verified_number_resolves_to_its_owner(self):
        from user.lookups import find_user

        # Both fields in one save - Profile.save() reads that as the
        # verification flow rather than a number change.
        self.squatter.profile.phone_no_verified = True
        self.squatter.profile.save()

        self.assertEqual(find_user(self.number), self.squatter)

    def test_a_squatter_cannot_be_added_to_a_group_by_the_real_number(self):
        from chat.models import Conversation
        from chat.serializers import resolve_members

        owner = User.objects.create_user(username='owner', password='x')
        Conversation.start_group(owner, name='Family')

        with self.assertRaises(Exception) as caught:
            resolve_members([self.number])
        # Reported as an unknown identifier, which is the honest answer: no
        # verified owner of that number exists.
        self.assertIn(self.number, str(caught.exception))

    def test_username_lookup_is_unaffected(self):
        from user.lookups import find_user

        self.assertEqual(find_user('squatter'), self.squatter)


class EmailLookupTests(APITestCase):
    """Addressing somebody by their email, the third form `find_user` takes.

    Symmetrical with the phone rule above, and for the same reason: `email` is
    not unique and any account can PATCH its address to one it does not own -
    that clears `email_verified` but leaves `is_active` True and its tokens
    working. Only a VERIFIED address resolves, or typing somebody's address
    into a group would add whoever squatted it.

    Inactive accounts stay invisible too - a banned account must answer the
    same way a nonexistent one does, or the lookup enumerates them.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='mara', email='Mara@Example.com', password='x'
        )
        self.user.profile.email_verified = True
        self.user.profile.save(update_fields=['email_verified'])

    def test_an_email_resolves_to_its_owner(self):
        from user.lookups import find_user

        self.assertEqual(find_user('mara@example.com'), self.user)

    def test_matching_ignores_case(self):
        from user.lookups import find_user

        self.assertEqual(find_user('MARA@EXAMPLE.COM'), self.user)

    def test_an_unknown_email_resolves_to_nobody(self):
        from user.lookups import find_user

        self.assertIsNone(find_user('nobody@example.com'))

    def test_an_inactive_account_is_invisible(self):
        from user.lookups import find_user

        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertIsNone(find_user('mara@example.com'))

    def test_a_username_containing_an_at_still_resolves(self):
        """The email branch is a try, not a claim on the character.

        Django permits '@' in a username, so an account called `a@b` has to
        stay addressable after email lookup exists.
        """
        from user.lookups import find_user

        odd = User.objects.create_user(username='a@b', password='x')
        self.assertEqual(find_user('a@b'), odd)

    def test_exclude_still_applies(self):
        from user.lookups import find_user

        self.assertIsNone(find_user('mara@example.com', exclude=self.user))

    def test_a_group_can_be_created_with_an_email_member(self):
        from chat.models import Conversation
        from chat.serializers import resolve_members

        creator = User.objects.create_user(username='owner', password='x')
        Conversation.start_group(creator, name='Family')

        self.assertEqual(
            resolve_members(['mara@example.com'], exclude=creator), [self.user]
        )

    def test_an_unverified_address_resolves_to_nobody(self):
        """The squat. Registering and never opening the email is enough."""
        from user.lookups import find_user

        self.user.profile.email_verified = False
        self.user.profile.save(update_fields=['email_verified'])
        self.assertIsNone(find_user('mara@example.com'))

    def test_a_squatter_cannot_be_added_to_a_group_by_the_real_address(self):
        from chat.models import Conversation
        from chat.serializers import resolve_members

        squatter = User.objects.create_user(
            username='mallory', email='finance@acme.com', password='x'
        )
        squatter.profile.email_verified = False
        squatter.profile.save(update_fields=['email_verified'])

        owner = User.objects.create_user(username='ana', password='x')
        Conversation.start_group(owner, name='Private')

        with self.assertRaises(Exception) as caught:
            resolve_members(['finance@acme.com'], exclude=owner)
        self.assertIn('finance@acme.com', str(caught.exception))

    def test_an_unknown_email_names_itself_in_the_error(self):
        from chat.serializers import resolve_members

        with self.assertRaises(Exception) as caught:
            resolve_members(['ghost@example.com'])
        self.assertIn('ghost@example.com', str(caught.exception))


class EmailPreferenceTests(EnvelopeAssertions, APITestCase):
    """The account-wide switch for group email.

    Exposed, unlike its SMS sibling, and the asymmetry is the point: SMS has a
    carrier word for this and email does not, so without an endpoint there
    would be nowhere at all to turn group email off for every group at once.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='alice', password='a-strong-passphrase-42',
            email='alice@example.com',
        )
        self.client.force_authenticate(self.user)

    def url(self):
        return reverse('user:user-me-profile')

    def test_it_is_on_by_default_and_readable(self):
        data = self.assertSuccess(self.client.get(self.url()))

        self.assertTrue(data['email_enabled'])

    def test_it_can_be_turned_off(self):
        data = self.assertSuccess(
            self.client.patch(self.url(), {'email_enabled': False})
        )

        self.assertFalse(data['email_enabled'])
        self.assertFalse(MessagingSettings.objects.get(user=self.user).email_enabled)

    def test_and_back_on(self):
        self.client.patch(self.url(), {'email_enabled': False})

        data = self.assertSuccess(
            self.client.patch(self.url(), {'email_enabled': True})
        )

        self.assertTrue(data['email_enabled'])

    def test_turning_it_off_records_when_and_that_they_did_it(self):
        self.assertSuccess(self.client.patch(self.url(), {'email_enabled': False}))

        switches = MessagingSettings.objects.get(user=self.user)
        self.assertIsNotNone(switches.email_opt_out_at)
        self.assertEqual(
            switches.email_opt_out_reason, MessagingSettings.EmailOptOutReason.PERSON
        )

    def test_turning_it_back_on_keeps_the_record(self):
        # "Opted out, then back in" is a different fact from "never opted out".
        self.client.patch(self.url(), {'email_enabled': False})
        turned_off_at = MessagingSettings.objects.get(user=self.user).email_opt_out_at

        self.client.patch(self.url(), {'email_enabled': True})

        switches = MessagingSettings.objects.get(user=self.user)
        self.assertTrue(switches.email_enabled)
        self.assertEqual(switches.email_opt_out_at, turned_off_at)

    def test_it_does_not_disturb_anything_else(self):
        self.assertSuccess(
            self.client.patch(self.url(), {'email_enabled': False})
        )

        self.assertTrue(MessagingSettings.objects.get(user=self.user).sms_enabled)

    def test_the_sms_switch_is_still_not_writable(self):
        # Its absence is deliberate: the only thing that writes it is a carrier
        # STOP. DRF discards an unknown field rather than refusing it, so this
        # asserts the outcome and not the status code.
        self.client.patch(self.url(), {'sms_enabled': False})

        self.assertTrue(MessagingSettings.objects.get(user=self.user).sms_enabled)

    def test_turning_group_email_off_does_not_stop_a_verification_code(self):
        # An account that cannot receive the code to get back into itself is an
        # account somebody has locked themselves out of, and nobody who unticks
        # "email updates" means that.
        from utils.mail import VERIFICATION_CODE, send_templated_email

        switches = self.user.messaging_settings
        switches.email_enabled = False
        switches.save(update_fields=['email_enabled'])

        with mock.patch('utils.mail._is_configured', return_value=True), \
                mock.patch('utils.mail._post', return_value={'id': 'x'}) as post:
            send_templated_email(
                VERIFICATION_CODE,
                to=self.user.email,
                context={'code': '123456', 'expires_in_minutes': 10},
            )

        self.assertEqual(post.call_count, 1)


@override_settings(STORAGES={
    **settings.STORAGES,
    # The admin's templates use {% static %}, and the manifest storage the
    # tests otherwise get raises without a collectstatic run.
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class UserAdminTests(TestCase):
    """The user page in Django admin, where staff look an account up."""

    def setUp(self):
        staff = User.objects.create_superuser('staff', 'staff@example.com', 'x')
        self.client.force_login(staff)
        self.alice = User.objects.create_user(username='alice', password='x')

    def test_it_shows_both_delivery_switches_for_staff_to_change(self):
        # The switches left Profile in user/0009, so without their own inline
        # they would vanish from this page. Staff may switch texts and group
        # email either way; the records of when each went off are view-only.
        # A STOP is per group and is not on this page at all.
        response = self.client.get(reverse('admin:auth_user_change', args=[self.alice.pk]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, 'name="messaging_settings-0-sms_enabled"')
        self.assertContains(response, 'name="messaging_settings-0-email_enabled"')
        self.assertNotContains(response, 'name="messaging_settings-0-sms_opt_out_at"')

    def test_it_labels_the_switches_in_the_settings_screens_words(self):
        # Field names are older than the screen; staff should read the words
        # the person reads.
        response = self.client.get(reverse('admin:auth_user_change', args=[self.alice.pk]))

        self.assertContains(response, 'Send and receive Loop messages by text')
        self.assertContains(response, 'Send and receive Loop messages by email')
        self.assertNotContains(response, 'Sms enabled')

    def save_messaging_settings_as_staff(self, *unticked):
        """Save the user page's Messaging settings as staff, every box ticked
        except those named - through the admin's own inline formset and
        save_formset, the path the page's Save button takes."""
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        request = RequestFactory().post('/')
        request.user = User.objects.get(username='staff')
        model_admin = django_admin.site._registry[User]
        inline = next(
            instance for instance in model_admin.get_inline_instances(request, self.alice)
            if instance.model is MessagingSettings
        )
        FormSet = inline.get_formset(request, self.alice)
        prefix = FormSet.get_default_prefix()
        switches = self.alice.messaging_settings
        data = {
            f'{prefix}-TOTAL_FORMS': '1',
            f'{prefix}-INITIAL_FORMS': '1',
            f'{prefix}-MIN_NUM_FORMS': '0',
            f'{prefix}-MAX_NUM_FORMS': '1',
            f'{prefix}-0-id': str(switches.pk),
            f'{prefix}-0-user': str(self.alice.pk),
        }
        for box in ('sms_enabled', 'email_enabled', 'enter_to_send', 'link_previews_enabled'):
            # An unticked box sends nothing at all.
            if box not in unticked:
                data[f'{prefix}-0-{box}'] = 'on'
        formset = FormSet(data, instance=self.alice, prefix=prefix)
        self.assertTrue(formset.is_valid(), formset.errors)

        model_admin.save_formset(request, None, formset, change=True)

        switches.refresh_from_db()
        return switches

    def test_staff_turning_texts_off_is_recorded(self):
        switches = self.save_messaging_settings_as_staff('sms_enabled')

        self.assertFalse(switches.sms_enabled)
        self.assertIsNotNone(switches.sms_opt_out_at)
        self.assertTrue(switches.email_enabled)

    def test_staff_turning_group_email_off_is_recorded_as_staff(self):
        switches = self.save_messaging_settings_as_staff('email_enabled')

        self.assertFalse(switches.email_enabled)
        self.assertIsNotNone(switches.email_opt_out_at)
        self.assertEqual(
            switches.email_opt_out_reason, MessagingSettings.EmailOptOutReason.STAFF
        )


class UserBlockTests(EnvelopeAssertions, APITestCase):
    """Blocking a person, and what it does and does not reach.

    The rule under test is narrow on purpose: this release blocks DIRECT
    contact only. A shared group is untouched - both people stay members, both
    still see each other's messages there - and there is a test below that pins
    that, because the easy mistake is to make a block quietly remove somebody.
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', password='a-strong-passphrase-42'
        )
        self.bob = User.objects.create_user(
            username='bob', password='a-strong-passphrase-42'
        )
        self.client.force_authenticate(self.alice)

    def block(self, **payload):
        return self.client.post(reverse('user:user-me-block'), payload, format='json')

    def blocked_rows(self):
        from user.models import UserBlock

        return UserBlock.objects.filter(blocker=self.alice)

    def unblock(self, **payload):
        return self.client.post(
            reverse('user:user-me-unblock'), payload, format='json'
        )

    def blocked_list(self):
        return self.assertSuccess(self.client.get(reverse('user:user-me-blocks')))

    def search(self, term):
        """Usernames the search returns. It is paginated, so unwrap it."""
        data = self.assertSuccess(
            self.client.get(reverse('user:user-search'), {'q': term})
        )
        rows = data['results'] if isinstance(data, dict) else data
        return [row['username'] for row in rows]

    # --- the record ---------------------------------------------------------

    def test_blocking_records_it(self):
        self.assertEqual(self.block(username='bob').status_code, 204)

        entries = self.blocked_list()
        self.assertEqual([e['username'] for e in entries], ['bob'])

    def test_blocking_twice_is_one_row(self):
        self.block(username='bob')
        self.assertEqual(self.block(username='bob').status_code, 204)

        self.assertEqual(len(self.blocked_list()), 1)

    def test_unblocking_removes_it(self):
        self.block(username='bob')

        self.assertEqual(self.unblock(username='bob').status_code, 204)

        self.assertEqual(self.blocked_list(), [])

    def test_unblocking_somebody_who_was_never_blocked_is_fine(self):
        self.assertEqual(self.unblock(username='bob').status_code, 204)

    def test_a_bare_number_can_be_blocked(self):
        # Most members of most groups have no account at all, so a block that
        # could only name accounts could not name most people.
        self.assertEqual(
            self.block(phone_number='+639171234567').status_code, 204
        )

        entries = self.blocked_list()
        self.assertEqual(entries[0]['phone_number'], '+639171234567')
        self.assertIsNone(entries[0]['username'])

    def test_you_cannot_block_yourself(self):
        self.assertFailure(self.block(username='alice'))

    def test_naming_both_or_neither_is_refused(self):
        self.assertFailure(self.block())
        self.assertFailure(self.block(username='bob', phone_number='+639171234567'))

    def test_my_list_is_mine_alone(self):
        # Nothing anywhere tells one person that another has blocked them.
        self.block(username='bob')
        self.client.force_authenticate(self.bob)

        self.assertEqual(self.blocked_list(), [])

    # --- a number becomes a person -------------------------------------------

    def verify(self, user, number):
        """Verify a number against an account, the way otp/services does."""
        from user import blocks

        profile = user.profile
        profile.phone_number = number
        profile.phone_no_verified = True
        profile.save(update_fields=['phone_number', 'phone_no_verified'])
        return blocks.link_number_to_account(user)

    def test_a_number_block_follows_the_number_onto_its_account(self):
        # Otherwise signing up is a clean way back in: block the number, they
        # register, and the block names an identity nobody uses any more.
        from user import blocks

        self.block(phone_number='+639171234567')

        self.assertEqual(self.verify(self.bob, '+639171234567'), 1)

        self.assertTrue(blocks.blocks_between(self.alice, self.bob))

    def test_and_the_row_MOVES_rather_than_being_copied(self):
        # "The block follows the account, not future owners of the number."
        # So nothing is left against the number for the next person to inherit.
        from user.models import UserBlock

        self.block(phone_number='+639171234567')
        self.verify(self.bob, '+639171234567')

        rows = UserBlock.objects.filter(blocker=self.alice)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().blocked_user_id, self.bob.pk)
        self.assertIsNone(rows.first().blocked_phone_number)

    def test_so_the_next_owner_of_that_number_is_not_blocked(self):
        from user import blocks

        self.block(phone_number='+639171234567')
        self.verify(self.bob, '+639171234567')

        # The carrier reassigns it. A stranger is not the person anybody
        # blocked, and inheriting a handset must not inherit a block.
        self.bob.profile.phone_number = None
        self.bob.profile.save(update_fields=['phone_number'])
        carol = User.objects.create_user(username='carol', password='x')
        self.verify(carol, '+639171234567')

        self.assertFalse(blocks.blocks_between(self.alice, carol))
        self.assertTrue(blocks.blocks_between(self.alice, self.bob))

    def test_blocking_my_own_number_just_goes_away(self):
        # People do this by typo, or by blocking a number they later claim.
        # A self-block is refused by a constraint and means nothing.
        from user.models import UserBlock

        self.block(phone_number='+639171234567')

        self.verify(self.alice, '+639171234567')

        self.assertFalse(UserBlock.objects.filter(blocker=self.alice).exists())

    def test_a_duplicate_is_collapsed_rather_than_doubled(self):
        # Blocking both the account and its number leaves ONE row - two would
        # make unblocking a guess about which to delete.
        from user.models import UserBlock

        self.block(username='bob')
        self.block(phone_number='+639171234567')

        self.verify(self.bob, '+639171234567')

        self.assertEqual(UserBlock.objects.filter(blocker=self.alice).count(), 1)

    def test_running_it_twice_changes_nothing(self):
        from user import blocks
        from user.models import UserBlock

        self.block(phone_number='+639171234567')
        self.verify(self.bob, '+639171234567')

        self.assertEqual(blocks.link_number_to_account(self.bob), 0)
        self.assertEqual(UserBlock.objects.filter(blocker=self.alice).count(), 1)

    def test_an_unverified_number_moves_nothing(self):
        from user import blocks
        from user.models import UserBlock

        self.block(phone_number='+639171234567')
        profile = self.bob.profile
        profile.phone_number = '+639171234567'
        profile.save(update_fields=['phone_number'])

        self.assertEqual(blocks.link_number_to_account(self.bob), 0)
        self.assertIsNotNone(
            UserBlock.objects.get(blocker=self.alice).blocked_phone_number
        )

    # --- search -------------------------------------------------------------

    def test_a_blocked_person_is_not_in_my_search(self):
        self.block(username='bob')

        self.assertNotIn('bob', self.search('bo'))

    def test_and_i_am_not_in_theirs_either(self):
        # Symmetric. A one-way block is observable: B would notice their own
        # blocks behaving differently.
        self.block(username='bob')
        self.client.force_authenticate(self.bob)

        self.assertNotIn('alice', self.search('al'))

    def test_somebody_unrelated_is_still_found(self):
        self.block(username='bob')
        User.objects.create_user(username='bobby', password='x')

        self.assertEqual(self.search('bob'), ['bobby'])


class SettingsModelTests(TestCase):
    """One settings row per screen, per user."""

    SETTINGS_MODELS = SETTINGS_MODELS

    def setUp(self):
        self.user = User.objects.create_user('settings-owner', password='Str0ng-pass!')

    def test_for_user_creates_one_valid_row_and_then_reuses_it(self):
        for model in self.SETTINGS_MODELS:
            with self.subTest(model=model.__name__):
                row = model.for_user(self.user)
                row.full_clean()
                self.assertEqual(model.for_user(self.user).pk, row.pk)
                self.assertEqual(model.objects.filter(user=self.user).count(), 1)

    def test_defaults_match_the_settings_screens(self):
        notifications = NotificationSettings.for_user(self.user)
        self.assertTrue(all(
            getattr(notifications, name) for name in (
                'messages', 'group_messages', 'direct_messages', 'mentions_replies',
                'group_invitations', 'incoming_calls', 'missed_calls',
                'sms_delivery_failures', 'email_delivery_failures', 'show_preview',
            )
        ))
        self.assertEqual(notifications.sound, NotificationSettings.Sound.DEFAULT)

        privacy = PrivacySettings.for_user(self.user)
        self.assertEqual(privacy.direct_messages_from, PrivacySettings.Audience.EVERYONE)
        self.assertEqual(privacy.group_adds_from, PrivacySettings.Audience.EVERYONE)

        messaging = MessagingSettings.for_user(self.user)
        self.assertTrue(messaging.enter_to_send)
        self.assertTrue(messaging.sms_enabled)

        security = SecuritySettings.for_user(self.user)
        self.assertFalse(security.two_factor_enabled)
        self.assertEqual(security.two_factor_method, '')

        appearance = AppearanceSettings.for_user(self.user)
        self.assertEqual(appearance.theme, AppearanceSettings.Theme.SYSTEM)
        self.assertEqual(appearance.text_size, AppearanceSettings.TextSize.DEFAULT)

    def test_two_factor_cannot_be_on_without_a_method(self):
        security = SecuritySettings.for_user(self.user)
        security.two_factor_enabled = True
        with self.assertRaises(IntegrityError), transaction.atomic():
            security.save()

    def test_delivery_switches_live_on_messaging_settings_only(self):
        # One home each, where the delivery code reads it - never a second copy.
        profile_fields = {field.name for field in Profile._meta.get_fields()}
        for name in ('sms_enabled', 'sms_opt_out_at', 'sms_consent_at', 'email_enabled'):
            self.assertNotIn(name, profile_fields)
        switches = MessagingSettings.of(self.user)
        self.assertTrue(switches.sms_enabled)
        self.assertTrue(switches.email_enabled)

    def test_every_new_account_has_messaging_settings_at_once(self):
        # The delivery code reads every member's switches; a row made lazily
        # would mean a group send that writes.
        self.assertTrue(MessagingSettings.objects.filter(user=self.user).exists())

    def test_deleting_the_user_deletes_their_settings(self):
        for model in self.SETTINGS_MODELS:
            model.for_user(self.user)
        self.user.delete()
        for model in self.SETTINGS_MODELS:
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.exists())


class SettingsAtSignInTests(EnvelopeAssertions, APITestCase):
    """Every way of signing in leaves the account with all its settings rows."""

    def setUp(self):
        cache.clear()
        self.password = 'a-strong-passphrase-42'
        self.user = User.objects.create_user(
            username='yul', email='yul@example.com', password=self.password
        )
        self.user.profile.email_verified = True
        self.user.profile.save(update_fields=['email_verified'])

    def login(self):
        return self.client.post(
            reverse('user:user-login'), {'username': 'yul', 'password': self.password}
        )

    def assertHasEverySettingsRow(self, user):
        for model in SETTINGS_MODELS:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.filter(user=user).count(), 1)

    def rows_other_than_messaging(self, user):
        # MessagingSettings is made at registration by the signal, so it is
        # the one row an account has before it ever signs in.
        return [
            model.__name__ for model in SETTINGS_MODELS
            if model is not MessagingSettings and model.objects.filter(user=user).exists()
        ]

    def test_registering_alone_does_not_create_them(self):
        self.assertEqual(self.rows_other_than_messaging(self.user), [])

    def test_password_login_creates_them(self):
        self.assertSuccess(self.login())
        self.assertHasEverySettingsRow(self.user)

    def test_signing_in_again_keeps_the_same_rows_and_choices(self):
        self.assertSuccess(self.login())
        AppearanceSettings.objects.filter(user=self.user).update(
            theme=AppearanceSettings.Theme.DARK
        )

        self.assertSuccess(self.login())

        self.assertHasEverySettingsRow(self.user)
        self.assertEqual(
            AppearanceSettings.objects.get(user=self.user).theme,
            AppearanceSettings.Theme.DARK,
        )

    @override_settings(REQUIRE_EMAIL_VERIFICATION=True)
    def test_a_refused_login_creates_nothing(self):
        self.user.profile.email_verified = False
        self.user.profile.save(update_fields=['email_verified'])

        self.assertFailure(self.login(), status.HTTP_401_UNAUTHORIZED)

        self.assertEqual(self.rows_other_than_messaging(self.user), [])

    @override_settings(REQUIRE_EMAIL_VERIFICATION=True)
    def test_verify_email_creates_them(self):
        self.client.post(
            reverse('user:user-register'),
            {
                'username': 'mara',
                'email': 'mara@example.com',
                'password': self.password,
                'confirm_password': self.password,
            },
        )
        mara = User.objects.get(username='mara')
        self.assertEqual(self.rows_other_than_messaging(mara), [])
        code = EmailOtp.objects.get(profile=mara.profile).code

        self.assertSuccess(
            self.client.post(
                reverse('user:user-verify-email'),
                {'email': 'mara@example.com', 'code': code},
            )
        )

        self.assertHasEverySettingsRow(mara)

    @patch('user.serializers.id_token.verify_oauth2_token')
    def test_google_sign_in_creates_them(self, verify_token):
        verify_token.return_value = {
            'sub': 'google-subject-456',
            'email': 'ana@example.com',
            'email_verified': True,
            'name': 'Ana',
        }

        self.assertSuccess(
            self.client.post(reverse('user:user-google'), {'credential': 'google-id-token'}),
            status.HTTP_201_CREATED,
        )

        self.assertHasEverySettingsRow(User.objects.get(email='ana@example.com'))


class SettingsApiTests(EnvelopeAssertions, APITestCase):
    """GET and PATCH /users/me/settings/ - the signed-in user's own settings."""

    def setUp(self):
        self.user = User.objects.create_user('yul', 'yul@example.com', 'x')
        self.client.force_authenticate(self.user)

    def url(self, screen=None):
        if screen is None:
            return reverse('user:settings-list')
        return reverse('user:settings-detail', kwargs={'screen': screen})

    def patch(self, screen, body):
        return self.client.patch(self.url(screen), body, format='json')

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertFailure(self.client.get(self.url()), status.HTTP_401_UNAUTHORIZED)
        self.assertFailure(
            self.patch('privacy', {'read_receipts_enabled': False}),
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_every_settings_model_has_a_screen(self):
        self.assertEqual(
            {serializer.Meta.model for serializer in SETTINGS_SERIALIZERS.values()},
            set(SETTINGS_MODELS),
        )

    def test_get_all_returns_every_screen_with_its_defaults(self):
        data = self.assertSuccess(self.client.get(self.url()))

        self.assertEqual(set(data), set(SETTINGS_SERIALIZERS))
        self.assertEqual(data['appearance']['theme'], 'system')
        self.assertTrue(data['messaging']['email_enabled'])
        self.assertTrue(data['notifications']['messages'])
        self.assertEqual(data['privacy']['direct_messages_from'], 'everyone')
        self.assertFalse(data['security']['two_factor_enabled'])

    def test_get_one_screen_makes_a_missing_row(self):
        self.assertFalse(PrivacySettings.objects.filter(user=self.user).exists())

        data = self.assertSuccess(self.client.get(self.url('privacy')))

        self.assertTrue(data['read_receipts_enabled'])
        self.assertTrue(PrivacySettings.objects.filter(user=self.user).exists())

    def test_the_payload_carries_no_ids_or_records(self):
        data = self.assertSuccess(self.client.get(self.url('messaging')))
        for key in ('id', 'user', 'created_at', 'sms_opt_out_at', 'sms_consent_at'):
            self.assertNotIn(key, data)

    def test_patch_changes_only_the_fields_sent(self):
        response = self.patch('appearance', {'theme': 'dark'})
        data = self.assertSuccess(response)

        self.assertEqual(response.json()['message'], 'Settings updated.')
        self.assertEqual(data['theme'], 'dark')
        self.assertEqual(data['text_size'], 'default')
        self.assertEqual(AppearanceSettings.objects.get(user=self.user).theme, 'dark')

    def test_an_unknown_choice_is_refused(self):
        body = self.assertFailure(self.patch('privacy', {'group_adds_from': 'friends'}))

        self.assertIn('group_adds_from', body['data'])
        self.assertEqual(PrivacySettings.for_user(self.user).group_adds_from, 'everyone')

    def test_an_unknown_screen_is_404(self):
        self.assertFailure(self.client.get(self.url('billing')), status.HTTP_404_NOT_FOUND)
        self.assertFailure(self.patch('billing', {'theme': 'dark'}), status.HTTP_404_NOT_FOUND)

    def test_put_is_not_allowed(self):
        self.assertFailure(
            self.client.put(self.url('appearance'), {'theme': 'dark'}, format='json'),
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def test_sms_enabled_is_the_persons_to_change(self):
        # "Send and receive Loop messages by text" - their own switch for every
        # Loop. A STOP is per group and never shows up here.
        data = self.assertSuccess(self.patch('messaging', {'sms_enabled': False}))

        self.assertFalse(data['sms_enabled'])
        switches = MessagingSettings.objects.get(user=self.user)
        self.assertFalse(switches.sms_enabled)
        self.assertIsNotNone(switches.sms_opt_out_at)

        data = self.assertSuccess(self.patch('messaging', {'sms_enabled': True}))
        self.assertTrue(data['sms_enabled'])

    def test_two_factor_cannot_be_switched_on_here(self):
        data = self.assertSuccess(self.patch('security', {
            'two_factor_enabled': True,
            'two_factor_method': 'sms',
            'login_alerts_email': True,
        }))

        self.assertFalse(data['two_factor_enabled'])
        self.assertEqual(data['two_factor_method'], '')
        self.assertTrue(data['login_alerts_email'])

    def test_turning_email_off_here_records_when_and_why(self):
        self.assertSuccess(self.patch('messaging', {'email_enabled': False}))
        first = MessagingSettings.objects.get(user=self.user)
        self.assertEqual(
            first.email_opt_out_reason, MessagingSettings.EmailOptOutReason.PERSON
        )
        self.assertIsNotNone(first.email_opt_out_at)

        # Off again while already off is not a second opt-out.
        self.assertSuccess(self.patch('messaging', {'email_enabled': False}))
        self.assertEqual(
            MessagingSettings.objects.get(user=self.user).email_opt_out_at,
            first.email_opt_out_at,
        )

    def test_email_enabled_is_the_same_switch_the_profile_shows(self):
        self.assertSuccess(self.patch('messaging', {'email_enabled': False}))

        profile = self.assertSuccess(self.client.get(reverse('user:user-me-profile')))
        self.assertFalse(profile['email_enabled'])

    def test_a_change_touches_only_the_callers_row(self):
        other = User.objects.create_user('mara', password='x')

        self.assertSuccess(self.patch('notifications', {'messages': False}))

        self.assertFalse(NotificationSettings.for_user(self.user).messages)
        self.assertTrue(NotificationSettings.for_user(other).messages)


class DeliverySwitchMigrationTests(TransactionTestCase):
    """user/0009 must carry every account's delivery switches across.

    Run against the real migration with the historical models, because a STOP
    that did not survive the move would be a compliance failure nobody sees.
    """

    before = [('user', '0008_settings_models')]
    after = [('user', '0009_move_delivery_switches_to_messaging_settings')]

    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.before)
        self.executor.loader.build_graph()

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_opt_outs_are_copied_and_missing_rows_created(self):
        old = self.executor.loader.project_state(self.before).apps
        OldUser = old.get_model('auth', 'User')
        OldProfile = old.get_model('user', 'Profile')
        stopped_at = timezone.now()

        # Historical models fire no signals, so neither account has a
        # MessagingSettings row yet - the migration has to make both.
        stopped = OldUser.objects.create(username='stopped-before-the-move')
        OldProfile.objects.create(
            user=stopped, sms_enabled=False, sms_opt_out_at=stopped_at, email_enabled=False,
        )
        untouched = OldUser.objects.create(username='never-opted-out')
        OldProfile.objects.create(user=untouched)

        self.executor.migrate(self.after)
        new = self.executor.loader.project_state(self.after).apps
        NewMessagingSettings = new.get_model('user', 'MessagingSettings')

        row = NewMessagingSettings.objects.get(user_id=stopped.pk)
        self.assertFalse(row.sms_enabled)
        self.assertFalse(row.email_enabled)
        self.assertEqual(row.sms_opt_out_at, stopped_at)

        other = NewMessagingSettings.objects.get(user_id=untouched.pk)
        self.assertTrue(other.sms_enabled)
        self.assertTrue(other.email_enabled)


class OldProfileColumnsDropTests(TransactionTestCase):
    """user/0012 carries any stray opt-out off Profile, then drops the columns.

    Against the real migration. The stray opt-outs it exists for were written
    by old code straight into those columns, so they are set here the same way,
    with raw SQL - the historical Profile model no longer has the fields.
    """

    before = [('user', '0011_email_opt_out_record')]
    after = [('user', '0012_drop_old_profile_delivery_columns')]
    OLD_COLUMNS = {'sms_enabled', 'sms_opt_out_at', 'sms_consent_at', 'email_enabled'}

    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.before)
        self.executor.loader.build_graph()

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def account(self, username, **old_columns):
        """An account with both rows, and these values in Profile's old columns."""
        old = self.executor.loader.project_state(self.before).apps
        user = old.get_model('auth', 'User').objects.create(username=username)
        old.get_model('user', 'Profile').objects.create(user=user)
        switches = old.get_model('user', 'MessagingSettings').objects.create(user=user)
        if old_columns:
            assignments = ', '.join(f'{name} = %s' for name in old_columns)
            with connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE user_profile SET {assignments} WHERE user_id = %s',
                    [*old_columns.values(), user.pk],
                )
        return user, switches

    def settings_after(self, user):
        new = self.executor.loader.project_state(self.after).apps
        return new.get_model('user', 'MessagingSettings').objects.get(user_id=user.pk)

    def profile_columns(self):
        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(
                cursor, 'user_profile'
            )
        return {column.name for column in description}

    def test_stray_opt_outs_are_carried_over_and_the_columns_dropped(self):
        stopped_at = (timezone.now() - timedelta(days=2)).replace(microsecond=0)
        stray_stop, _ = self.account(
            'stray-stop', sms_enabled=False, sms_opt_out_at=stopped_at
        )
        stray_email, _ = self.account('stray-email', email_enabled=False)
        untouched, _ = self.account('untouched')

        self.executor.migrate(self.after)

        stop = self.settings_after(stray_stop)
        self.assertFalse(stop.sms_enabled)
        self.assertEqual(stop.sms_opt_out_at, stopped_at)

        email = self.settings_after(stray_email)
        self.assertFalse(email.email_enabled)
        self.assertIsNotNone(email.email_opt_out_at)

        other = self.settings_after(untouched)
        self.assertTrue(other.sms_enabled)
        self.assertTrue(other.email_enabled)

        self.assertFalse(self.OLD_COLUMNS & self.profile_columns())

    def test_a_start_since_the_old_stop_is_left_on(self):
        # STOP before 0009, START after it: Profile still says off, and
        # MessagingSettings is right to say on.
        stopped_at = (timezone.now() - timedelta(days=2)).replace(microsecond=0)
        user, switches = self.account(
            'started-again', sms_enabled=False, sms_opt_out_at=stopped_at
        )
        switches.sms_consent_at = stopped_at + timedelta(days=1)
        switches.save(update_fields=['sms_consent_at'])

        self.executor.migrate(self.after)

        self.assertTrue(self.settings_after(user).sms_enabled)

    def test_rolling_back_puts_the_columns_back(self):
        self.executor.migrate(self.after)
        self.executor.loader.build_graph()

        self.executor.migrate(self.before)

        self.assertTrue(self.OLD_COLUMNS <= self.profile_columns())


class SmsSwitchMigrationTests(TransactionTestCase):
    """user/0013 makes `sms_enabled` the person's own switch.

    What they chose in the app becomes the switch. An account that only a STOP
    had switched off comes back on: the STOP is kept on the group it was sent to.
    """

    before = [('user', '0012_drop_old_profile_delivery_columns')]
    after = [('user', '0013_sms_switch_is_the_persons')]

    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.before)
        self.executor.loader.build_graph()

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_the_persons_choice_becomes_the_switch(self):
        old = self.executor.loader.project_state(self.before).apps
        OldUser = old.get_model('auth', 'User')
        OldSettings = old.get_model('user', 'MessagingSettings')
        app_only = OldUser.objects.create(username='app-only')
        OldSettings.objects.create(user=app_only, sms_to_loopd_users_enabled=False)
        stopped = OldUser.objects.create(username='stopped-somewhere')
        OldSettings.objects.create(
            user=stopped, sms_enabled=False, sms_opt_out_at=timezone.now()
        )
        untouched = OldUser.objects.create(username='untouched')
        OldSettings.objects.create(user=untouched)

        self.executor.migrate(self.after)
        new = self.executor.loader.project_state(self.after).apps
        NewSettings = new.get_model('user', 'MessagingSettings')

        chose_app_only = NewSettings.objects.get(user_id=app_only.pk)
        self.assertFalse(chose_app_only.sms_enabled)
        self.assertIsNotNone(chose_app_only.sms_opt_out_at)
        self.assertTrue(NewSettings.objects.get(user_id=stopped.pk).sms_enabled)
        self.assertTrue(NewSettings.objects.get(user_id=untouched.pk).sms_enabled)


class PresenceTests(TestCase):
    """Online while the app holds a socket, and stale presence reads offline."""

    def setUp(self):
        # The socket counter lives in the cache, which is per-process in tests.
        cache.clear()
        self.user = User.objects.create_user(username='presence', password='x')

    def profile(self):
        return Profile.objects.get(user=self.user)

    def stale(self):
        from user import presence

        Profile.objects.filter(user=self.user).update(
            last_seen_at=timezone.now() - presence.STALE_AFTER - timedelta(seconds=1)
        )

    def test_the_first_socket_marks_them_online(self):
        from user import presence

        self.assertTrue(presence.connected(self.user))
        self.assertTrue(presence.is_online(self.profile()))

    def test_a_second_device_is_not_news(self):
        from user import presence

        presence.connected(self.user)

        self.assertFalse(presence.connected(self.user))

    def test_only_the_last_socket_closing_takes_them_offline(self):
        from user import presence

        presence.connected(self.user)
        presence.connected(self.user)

        self.assertFalse(presence.disconnected(self.user))
        self.assertTrue(presence.is_online(self.profile()))

        self.assertTrue(presence.disconnected(self.user))
        self.assertFalse(self.profile().is_online)

    def test_presence_with_no_recent_beat_reads_offline(self):
        # A worker that dies never runs disconnect; nothing may stay online.
        from user import presence

        presence.connected(self.user)
        self.stale()

        self.assertFalse(presence.is_online(self.profile()))

    def test_a_beat_refreshes_it_and_then_throttles(self):
        from user import presence

        presence.connected(self.user)
        self.stale()

        self.assertTrue(presence.beat(self.user))
        self.assertTrue(presence.is_online(self.profile()))
        # Every socket of every signed-in person beats; one write each is plenty.
        self.assertFalse(presence.beat(self.user))

    def test_nobody_is_told_about_somebody_who_hides_their_status(self):
        from user import presence

        row = PrivacySettings.for_user(self.user)
        row.online_status_visible = False
        row.save(update_fields=['online_status_visible'])

        self.assertEqual(presence.frames(self.user, online=True), ([], None))


class DiscoverabilityTests(TestCase):
    """Settings → Privacy → Find me using my phone number / my email.

    Both switches are about the identifiers people hand out. Neither hides a
    @username: somebody who types that still finds them.
    """

    def setUp(self):
        self.hidden = User.objects.create_user(
            username='hidden', email='hidden@example.com', password='x'
        )
        self.hidden.profile.phone_number = '+639171111111'
        self.hidden.profile.phone_no_verified = True
        self.hidden.profile.email_verified = True
        self.hidden.profile.save()
        self.seeker = User.objects.create_user(username='seeker', password='x')

    def hide(self, **flags):
        row = PrivacySettings.for_user(self.hidden)
        for field, value in flags.items():
            setattr(row, field, value)
        row.save(update_fields=list(flags))

    def test_by_default_the_number_and_the_address_both_find_them(self):
        from user.lookups import find_user

        self.assertEqual(find_user('+639171111111', exclude=self.seeker), self.hidden)
        self.assertEqual(
            find_user('hidden@example.com', exclude=self.seeker), self.hidden
        )

    def test_a_hidden_number_finds_nobody(self):
        from user.lookups import find_user, search_users

        self.hide(discoverable_by_phone=False)

        self.assertIsNone(find_user('+639171111111', exclude=self.seeker))
        self.assertEqual(
            list(search_users('+639171111111', exclude=self.seeker)), []
        )

    def test_a_hidden_address_finds_nobody(self):
        from user.lookups import find_user

        self.hide(discoverable_by_email=False)

        self.assertIsNone(find_user('hidden@example.com', exclude=self.seeker))

    def test_their_username_still_finds_them(self):
        from user.lookups import find_user, search_users

        self.hide(discoverable_by_phone=False, discoverable_by_email=False)

        self.assertEqual(find_user('hidden', exclude=self.seeker), self.hidden)
        self.assertIn(self.hidden, search_users('hid', exclude=self.seeker))

    def test_the_one_caller_that_may_look_past_it_is_the_email_invite(self):
        # chat/membership.py:invite_by_email asks a second time, only to avoid
        # emailing an invitation that could never be claimed.
        from user.lookups import find_user

        self.hide(discoverable_by_email=False)

        self.assertEqual(
            find_user('hidden@example.com', respect_discoverability=False),
            self.hidden,
        )

