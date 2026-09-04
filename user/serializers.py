"""Serializers for accounts and profiles.

Read shapes (`UserSerializer`, `ProfileSerializer`) are deliberately separate
from write shapes (`RegisterSerializer`, `LoginSerializer`): the fields a
client sends when signing up have almost nothing in common with the fields it
gets back, and merging them is how write-only fields leak.
"""
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import exceptions, serializers
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from otp.models import OtpError
from otp.services import start_email_verification, verify_email
from user.models import Profile
from utils.email import EmailError, send_email

logger = logging.getLogger(__name__)


class ProfileSerializer(serializers.ModelSerializer):
    """A profile as the owner sees it, and as it is written back.

    `avatar` is write-only and `avatar_url` read-only: the client uploads a
    file but wants a URL back, and returning the raw relative path would make
    every consumer build the URL itself.
    """

    avatar = serializers.ImageField(write_only=True, required=False, allow_null=True)
    avatar_url = serializers.SerializerMethodField()
    display_label = serializers.CharField(read_only=True)

    class Meta:
        model = Profile
        fields = [
            'display_name',
            'display_label',
            'avatar',
            'avatar_url',
            'phone_number',
            'phone_no_verified',
            'email_verified',
            'bio',
            'date_of_birth',
            'is_online',
            'last_seen_at',
            'updated_at',
        ]
        # Presence is owned by the websocket layer, not by whoever PATCHes.
        # phone_no_verified belongs to the verification flow; a client that
        # could PATCH it could mark its own unverified number verified.
        read_only_fields = [
            'phone_no_verified',
            'email_verified',
            'is_online',
            'last_seen_at',
            'updated_at',
        ]

    def get_avatar_url(self, obj):
        if not obj.avatar:
            return None
        request = self.context.get('request')
        # Absolute when there is a request to build it from, so mobile
        # clients do not have to know the host.
        return request.build_absolute_uri(obj.avatar.url) if request else obj.avatar.url

    def validate_avatar(self, value):
        # ImageField already proved it decodes as an image; this is about
        # not letting a 40MB photo through.
        if value and value.size > settings.MAX_AVATAR_UPLOAD_BYTES:
            limit_mb = settings.MAX_AVATAR_UPLOAD_BYTES // (1024 * 1024)
            raise serializers.ValidationError(f'Keep the image under {limit_mb}MB.')
        return value

    def validate_phone_number(self, value):
        if not value:
            return None
        taken = Profile.objects.filter(phone_number=value)
        if self.instance:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise serializers.ValidationError('That phone number is already in use.')
        return value


class PublicProfileSerializer(ProfileSerializer):
    """What one user is allowed to see about another.

    Phone number and date of birth are contact details, not public facts, so
    looking someone up by id must not hand them over.
    """

    class Meta(ProfileSerializer.Meta):
        fields = ['display_label', 'avatar_url', 'bio', 'is_online', 'last_seen_at']
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    """The signed-in user, and the target of PATCH /api/users/me/."""

    profile = ProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'first_name',
            'last_name',
            'date_joined',
            'profile',
        ]
        # Username changes break @mentions and message attribution, so they
        # are not part of this endpoint.
        read_only_fields = ['id', 'username', 'date_joined']

    def validate_email(self, value):
        return _validate_unique_email(value, exclude_user=self.instance)

    def update(self, instance, validated_data):
        # Compared before the write, while `instance` still holds the old one.
        email_changed = 'email' in validated_data and (
            validated_data['email'].lower() != (instance.email or '').lower()
        )

        user = super().update(instance, validated_data)

        if email_changed:
            # The new address has not been confirmed, so the badge cannot
            # survive the change - the same rule Profile.save() applies when a
            # phone number changes. Without this, someone verifies an address
            # they own, swaps in one they do not, and keeps the flag.
            profile = user.profile
            profile.email_verified = False
            profile.save(update_fields=['email_verified', 'updated_at'])

        return user


class PublicUserSerializer(serializers.ModelSerializer):
    """Another user, as seen from a conversation."""

    profile = PublicProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'profile']
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    """Sign-up: creates the User and fills in the Profile the signal made."""

    password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
        # Runs AUTH_PASSWORD_VALIDATORS: length, commonness, all-numeric.
        validators=[validate_password],
    )
    confirm_password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
    )
    email = serializers.EmailField(required=True)

    # Profile fields accepted at sign-up so a new account is immediately
    # usable in a conversation list.
    display_name = serializers.CharField(max_length=80, required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=16, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            'username',
            'email',
            'password',
            'confirm_password',
            'first_name',
            'last_name',
            'display_name',
            'phone_number',
        ]

    def validate_username(self, value):
        value = value.strip()
        # Django's unique constraint is case-sensitive, which would let
        # "Yul" and "yul" both exist and confuse everyone in a chat.
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError('That username is taken.')
        return value

    def validate_email(self, value):
        return _validate_unique_email(value)

    def validate_phone_number(self, value):
        if not value:
            return ''
        from user.models import phone_number_validator

        phone_number_validator(value)
        if Profile.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError('That phone number is already in use.')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('confirm_password'):
            # Attached to the field so the client can highlight it.
            raise serializers.ValidationError(
                {'confirm_password': 'The two passwords do not match.'}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        profile_data = {
            'display_name': validated_data.pop('display_name', '') or '',
            'phone_number': validated_data.pop('phone_number', '') or None,
        }
        password = validated_data.pop('password')

        # create_user hashes the password; User(**data) would store it raw.
        #
        # is_active stays True even when the address is unverified: it means
        # "not banned", and nothing but an admin may write it. What actually
        # gates signing in is profile.email_verified, checked in
        # LoginSerializer. See the note on the field in user/models.py.
        user = User.objects.create_user(password=password, **validated_data)

        # The post_save signal already made the Profile; fill it in.
        #
        # email_verified is decided here, in the same place and from the same
        # setting as the rest of the account, so the two can never disagree.
        # With verification off nobody is ever asked for a code, so leaving it
        # False would mark every account unverified forever - and hand a banned
        # one a working route through resend-code.
        profile = user.profile
        profile.display_name = profile_data['display_name']
        profile.phone_number = profile_data['phone_number']
        profile.email_verified = not settings.REQUIRE_EMAIL_VERIFICATION
        profile.save(
            update_fields=[
                'display_name',
                'phone_number',
                'email_verified',
                'updated_at',
            ]
        )

        return user


class GoogleCredentialSerializer(serializers.Serializer):
    """Validates the ID token issued by Google Identity Services."""

    credential = serializers.CharField(trim_whitespace=True)

    def validate_credential(self, value):
        if not settings.GOOGLE_CLIENT_ID:
            raise serializers.ValidationError('Google sign-in is not configured.')

        try:
            claims = id_token.verify_oauth2_token(
                value,
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID,
            )
        except (GoogleAuthError, ValueError):
            raise serializers.ValidationError('The Google credential is invalid.')

        if not claims.get('email') or claims.get('email_verified') is not True:
            raise serializers.ValidationError('The Google account email is not verified.')

        return claims


class LoginSerializer(TokenObtainPairSerializer):
    """Username-or-email login returning an access/refresh pair."""

    default_error_messages = {
        # Deliberately identical for "no such user" and "wrong password" so
        # the endpoint cannot be used to enumerate accounts.
        'no_active_account': 'Incorrect username or password.',
    }

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Lets the websocket layer name a connection without a DB hit.
        token['username'] = user.username
        return token

    def validate(self, attrs):
        login = (attrs.get(self.username_field) or '').strip()

        # People type whichever they remember; translate an email to the
        # username that authenticate() expects.
        if '@' in login:
            match = User.objects.filter(email__iexact=login).first()
            if match:
                attrs[self.username_field] = match.username

        # A banned account (is_active=False) fails in here and is told only
        # 'Incorrect username or password.' - naming the ban would confirm the
        # account exists, and there is nothing the user could do about it.
        data = super().validate(attrs)

        # Past this point the password was correct, so saying why sign-in is
        # refused reveals nothing to anyone who did not already know it. That
        # is what keeps this from becoming an account-enumeration oracle.
        if settings.REQUIRE_EMAIL_VERIFICATION and not self.user.profile.email_verified:
            raise exceptions.AuthenticationFailed(
                'Verify your email address before signing in.',
                'email_not_verified',
            )

        # Reshaped to match register, so a client parses one thing
        # whether it just signed up or just logged in.
        return {
            'user': UserSerializer(self.user, context=self.context).data,
            'tokens': {'access': data['access'], 'refresh': data['refresh']},
        }



class VerifyEmailSerializer(serializers.Serializer):
    """Confirms an emailed code and activates the account."""

    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)

    def validate_email(self, value):
        # `is_active=True` in the lookup, not a separate check: a banned
        # account is simply not found, so it gets the same answer as an
        # address nobody uses. Telling it apart would confirm the account
        # exists, and a ban must not be undoable from out here anyway.
        user = User.objects.filter(
            email__iexact=value.strip(), is_active=True
        ).select_related('profile').first()
        if user is None:
            raise serializers.ValidationError('No account uses that email address.')
        if user.profile.email_verified:
            raise serializers.ValidationError('That account is already verified.')
        self.user = user
        return value

    def save(self, **kwargs):
        try:
            return verify_email(self.user, self.validated_data['code'])
        except OtpError as exc:
            # Wrong, expired or exhausted - the message from the otp app is
            # written to be shown to the user as-is.
            raise serializers.ValidationError({'code': str(exc)})


class ResendCodeSerializer(serializers.Serializer):
    """Issues a fresh verification code for an unverified account."""

    email = serializers.EmailField()

    def validate_email(self, value):
        # See the note in VerifyEmailSerializer: a banned account must not be
        # issued a code, and must not be able to tell that it is banned.
        user = User.objects.filter(
            email__iexact=value.strip(), is_active=True
        ).select_related('profile').first()
        if user is None:
            raise serializers.ValidationError('No account uses that email address.')
        if user.profile.email_verified:
            raise serializers.ValidationError('That account is already verified.')
        self.user = user
        return value

    def save(self, **kwargs):
        try:
            return start_email_verification(self.user)
        except OtpError as exc:
            # Almost always the resend cooldown.
            raise serializers.ValidationError({'email': str(exc)})


class LogoutSerializer(serializers.Serializer):
    """Blacklists one refresh token.

    The access token stays valid until it expires - that is the trade-off
    JWT makes - which is why ACCESS_TOKEN_LIFETIME is short.
    """

    refresh = serializers.CharField(write_only=True)

    def validate_refresh(self, value):
        try:
            self.token = RefreshToken(value)
        except TokenError:
            raise serializers.ValidationError('That refresh token is invalid or expired.')
        return value

    def save(self, **kwargs):
        self.token.blacklist()
        return self.token


class ForgotPasswordSerializer(serializers.Serializer):
    """Emails a password reset link for an account, given its address.

    Always succeeds from the caller's point of view - `save()` silently does
    nothing when the address has no active account, and the same when the
    address is over its rate limit - so this endpoint cannot be used to
    discover which addresses are registered, or that a limit exists at all.
    The same reasoning LoginSerializer uses for its one error message.
    """

    email = serializers.EmailField()

    def save(self, **kwargs):
        email = self.validated_data['email'].strip().lower()

        # Checked - and recorded - on the submitted address itself, before
        # the account lookup, so the limit applies identically whether or
        # not that address has an account. Nothing here can be used to tell
        # those two cases apart from the response.
        rate_limited = _password_reset_rate_limited(email)
        _record_password_reset_request(email)
        if rate_limited:
            return None

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            _send_password_reset_email(user)
        return user


class ResetPasswordSerializer(serializers.Serializer):
    """Consumes a reset link's uid/token and sets a new password.

    uid/token come from the query string of the link ForgotPasswordSerializer
    emailed - see `_send_password_reset_email` below. The token is checked
    with Django's own `default_token_generator`, so its lifetime is whatever
    `settings.PASSWORD_RESET_TIMEOUT` says (30 minutes here) with no separate
    token model or expiry column to keep in sync.
    """

    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, style={'input_type': 'password'})
    confirm_password = serializers.CharField(write_only=True, style={'input_type': 'password'})

    def validate(self, attrs):
        try:
            user_id = force_str(urlsafe_base64_decode(attrs['uid']))
            user = User.objects.get(pk=user_id, is_active=True)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise serializers.ValidationError({'uid': 'This reset link is invalid.'})

        # check_token also enforces the expiry, against PASSWORD_RESET_TIMEOUT -
        # there is nothing here to expire by hand.
        if not default_token_generator.check_token(user, attrs['token']):
            raise serializers.ValidationError(
                {'token': 'This reset link is invalid or has expired.'}
            )

        if attrs['new_password'] != attrs['confirm_password']:
            raise serializers.ValidationError(
                {'confirm_password': 'The two passwords do not match.'}
            )

        # Called here rather than as a field validator so that
        # UserAttributeSimilarityValidator has the user to compare against -
        # a field validator would run before `user` is known.
        try:
            validate_password(attrs['new_password'], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'new_password': exc.messages})

        self.user = user
        return attrs

    def save(self, **kwargs):
        self.user.set_password(self.validated_data['new_password'])
        self.user.save(update_fields=['password'])
        return self.user


class ChangePasswordSerializer(serializers.Serializer):
    """Changes the signed-in user's password. Requires the current one."""

    old_password = serializers.CharField(write_only=True, style={'input_type': 'password'})
    new_password = serializers.CharField(write_only=True, style={'input_type': 'password'})

    def validate_old_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Your current password is incorrect.')
        return value

    def validate_new_password(self, value):
        try:
            validate_password(value, user=self.context['request'].user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        return value

    def validate(self, attrs):
        if attrs['old_password'] == attrs['new_password']:
            raise serializers.ValidationError(
                {'new_password': 'Your new password must be different from your current one.'}
            )
        return attrs

    def save(self, **kwargs):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save(update_fields=['password'])
        return user


def _send_password_reset_email(user):
    """Build a dynamic reset link and email it.

    uid is the user's id, base64-encoded the way Django's own password-reset
    views do it; token is single-use in effect - `check_token` bakes the
    current password hash into what it verifies, so consuming the link
    (which calls `set_password`) invalidates it for any second attempt.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    reset_url = f'{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}'
    minutes = settings.PASSWORD_RESET_TIMEOUT // 60

    try:
        send_email(
            to=user.email,
            subject="Reset your Loopd'n password",
            html=(
                f'<p>Tap the link below to set a new password. '
                f'It expires in {minutes} minutes.</p>'
                f'<p><a href="{reset_url}">{reset_url}</a></p>'
                f'<p>If you did not request this, you can ignore this email.</p>'
            ),
        )
    except EmailError:
        # The provider failed, not the user, and the caller (forgot_password)
        # must answer identically either way - see ForgotPasswordSerializer -
        # so there is nothing useful to do with this here but log it.
        logger.warning('Password reset email to %s could not be sent.', user.email)


# --- forgot-password rate limiting ------------------------------------------
#
# Same cache-based fixed-window mechanism as otp/services.py's OTP send
# limits: a cooldown key stops an immediate double-submit, and an hourly
# counter stops a slow drip that waits the cooldown out each time. Keyed on
# the submitted email address itself (not a resolved user), which is what
# lets ForgotPasswordSerializer check - and record - a request the same way
# whether or not that address has an account.


def _password_reset_cache_keys(email):
    return (
        f'password_reset:cooldown:{email}',
        f'password_reset:h:{email}',
    )


def _password_reset_rate_limited(email):
    cooldown_key, hourly_key = _password_reset_cache_keys(email)
    if cache.get(cooldown_key) is not None:
        return True
    return (cache.get(hourly_key) or 0) >= settings.PASSWORD_RESET_MAX_REQUESTS_PER_HOUR


def _record_password_reset_request(email):
    cooldown_key, hourly_key = _password_reset_cache_keys(email)
    cache.set(cooldown_key, True, settings.PASSWORD_RESET_COOLDOWN_SECONDS)

    # add() only writes when the key is absent, which is what starts the
    # window; incr() then bumps it without extending the expiry - same
    # pattern as otp/services.py's _record_send.
    cache.add(hourly_key, 0, 3600)
    try:
        cache.incr(hourly_key)
    except ValueError:
        # Expired between the add and the incr. Losing one tick of the
        # window is harmless.
        cache.set(hourly_key, 1, 3600)


def _validate_unique_email(value, exclude_user=None):
    """Django's User.email has no unique constraint; this supplies one.

    Case-insensitive, because nobody thinks of Yul@x.com and yul@x.com as two
    accounts - and password reset would become ambiguous if they were.
    """
    value = value.strip().lower()
    taken = User.objects.filter(email__iexact=value)
    if exclude_user is not None:
        taken = taken.exclude(pk=exclude_user.pk)
    if taken.exists():
        raise serializers.ValidationError('An account with that email already exists.')
    return value
