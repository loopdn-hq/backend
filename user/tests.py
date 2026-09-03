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
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from otp.models import EmailOtp
from otp.services import verify_email
from user.models import Profile
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
