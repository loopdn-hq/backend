"""Version 1 of the user API.

One viewset covers the whole account lifecycle:

    POST   /api/v1/users/register/        create an account (unverified; must confirm)
    POST   /api/v1/users/verify-email/    confirm the code -> account verified
    POST   /api/v1/users/resend-code/     issue a fresh verification code
    POST   /api/v1/users/google/          sign in or create an account with Google
    POST   /api/v1/users/login/           username-or-email + password -> tokens
    POST   /api/v1/users/logout/          blacklist the caller's refresh token
    POST   /api/v1/users/token/refresh/   trade a refresh token for a new pair
    POST   /api/v1/users/forgot-password/ email a password reset link
    POST   /api/v1/users/reset-password/  consume the link's uid/token -> new password
    POST   /api/v1/users/change-password/ signed-in: old_password + new_password
    GET    /api/v1/users/me/              the signed-in account
    PATCH  /api/v1/users/me/              update account fields
    GET    /api/v1/users/me/profile/      the signed-in profile
    PATCH  /api/v1/users/me/profile/      update the profile (avatar included)
    GET    /api/v1/users/search/?q=      find people by username or phone number
    GET    /api/v1/users/<id>/            another user, as a conversation shows them

Nothing here builds a response body by hand: `loopd.renderers`
wraps every reply in the {code, message, data} envelope, so an action returns
its payload and names its `message` in `success_messages`.
"""
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from otp.models import OtpError
from otp.services import start_email_verification
from user import blocks
from user.lookups import search_users
from user.models import Profile, UserBlock
from user.serializers import (
    BlockedEntrySerializer,
    BlockSerializer,
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    GoogleCredentialSerializer,
    LoginSerializer,
    LogoutSerializer,
    ProfileSerializer,
    PublicUserSerializer,
    RegisterSerializer,
    ResendCodeSerializer,
    ResetPasswordSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)
from user.user_settings import ensure_settings
from utils.mail import EmailError

logger = logging.getLogger(__name__)


def issue_tokens(user):
    """The access/refresh pair, in the shape every auth action returns.

    Handing out tokens is signing in, so this is also where the account's
    settings rows are made (user/user_settings.py). Password login mints its
    tokens inside LoginSerializer and makes the same call there.
    """
    ensure_settings(user)
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


def verification_payload(otp):
    """What the client needs to drive the "enter your code" screen."""
    payload = {
        'email': otp.profile.user.email,
        'expires_at': otp.expiry,
        'resend_available_in': otp.seconds_until_resend,
    }

    # With Resend unconfigured nothing is actually delivered, so development
    # returns the code directly. Both halves of the guard matter: DEBUG alone
    # would hand a live, deliverable credential to anyone who can call this
    # endpoint with a known email address. Same two-part guard as the phone
    # equivalent in otp/viewsets/v1/otp.py.
    if settings.DEBUG and not settings.RESEND_API_KEY:
        payload['debug_code'] = otp.code

    return payload


class UserViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Accounts, sessions and profiles."""

    # Deactivated accounts stay in the table for message attribution, but
    # must not be retrievable.
    queryset = User.objects.select_related('profile').filter(is_active=True)
    serializer_class = PublicUserSerializer

    # The default from settings is IsAuthenticated; the three actions that
    # cannot require a token open themselves up individually below.
    permission_classes = [IsAuthenticated]

    # One serializer per action beats branching inside each method.
    serializer_classes = {
        'register': RegisterSerializer,
        'google': GoogleCredentialSerializer,
        'verify_email': VerifyEmailSerializer,
        'resend_code': ResendCodeSerializer,
        'login': LoginSerializer,
        'logout': LogoutSerializer,
        'refresh': TokenRefreshSerializer,
        'forgot_password': ForgotPasswordSerializer,
        'reset_password': ResetPasswordSerializer,
        'change_password': ChangePasswordSerializer,
        'me': UserSerializer,
        'profile': ProfileSerializer,
        'block': BlockSerializer,
        'unblock': BlockSerializer,
    }

    success_messages = {
        'register': 'Account created. Enter the verification code to activate it.',
        'google': 'Google sign-in successful.',
        'verify_email': 'Email verified. Your account is now active.',
        'resend_code': 'A new verification code has been issued.',
        'login': 'Login successful.',
        'logout': 'Logged out.',
        'refresh': 'Token refreshed.',
        'forgot_password': 'If an account exists for that email, a reset link has been sent.',
        'reset_password': 'Password reset. You can now log in with your new password.',
        'change_password': 'Password changed.',
        'me:GET': 'Account fetched.',
        'me:PATCH': 'Account updated.',
        'profile:GET': 'Profile fetched.',
        'profile:PATCH': 'Profile updated.',
        'retrieve': 'User fetched.',
        'search': 'Search complete.',
        'blocks:GET': 'Blocked list fetched.',
        'block': 'Blocked.',
        'unblock': 'Unblocked.',
    }

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.serializer_class)

    # -- accounts ---------------------------------------------------------

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def register(self, request):
        """Create an account.

        No tokens are ever returned here. Signing in is the login endpoint's
        job, so registering and authenticating stay separate steps and a
        client only has one place to get credentials from.

        With REQUIRE_EMAIL_VERIFICATION on (the current setting) the account
        is created UNVERIFIED and a code is emailed; login is refused until
        verify-email confirms it. With it off, the account is marked verified
        here and the caller can log in right away.

        The account is always `is_active=True` either way: that field means
        "not banned" and belongs to admins alone.

        A delivery failure does not fail the request - see below.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        data = {'user': UserSerializer(user, context=self.get_serializer_context()).data}

        if settings.REQUIRE_EMAIL_VERIFICATION:
            try:
                data['verification'] = verification_payload(
                    start_email_verification(user)
                )
                message = 'Account created. Enter the verification code to activate it.'
            except (EmailError, OtpError) as exc:
                # The account is already committed - RegisterSerializer.create
                # closed its transaction when save() returned - and only
                # delivery failed. Failing the whole request would strand the
                # user for good: registering again answers "that email already
                # exists", and a client that has just been shown an error has
                # no reason to call resend-code. So the account stands, and the
                # message says what to do about it.
                logger.warning(
                    'Verification email failed for %s: %s', user.email, exc
                )
                data['verification'] = None
                message = (
                    'Account created, but the verification code could not be '
                    'sent. Request a new one.'
                )
        else:
            message = 'Account created successfully. You can now log in.'

        response = Response(data, status=status.HTTP_201_CREATED)
        # The renderer prefers a message set on the response over the one in
        # success_messages, which cannot vary per request.
        response.message = message
        return response

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def google(self, request):
        """Exchange a verified Google ID token for this API's token pair."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user, created = self._get_or_create_google_user(
            serializer.validated_data['credential']
        )

        if not user.is_active:
            return Response(
                {'detail': 'This account is inactive.'}, status=status.HTTP_401_UNAUTHORIZED
            )

        response = Response(
            {
                'user': UserSerializer(user, context=self.get_serializer_context()).data,
                'tokens': issue_tokens(user),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
        if created:
            response.message = 'Google account created successfully.'
        return response

    @staticmethod
    @transaction.atomic
    def _get_or_create_google_user(claims):
        email = claims['email'].strip()
        user = User.objects.filter(email__iexact=email).first()
        if user:
            # Google is a verification of this address, and a stronger one
            # than our own six-digit code: GoogleCredentialSerializer refuses
            # any token whose email_verified claim is not True, so arriving
            # here means Google has vouched for it. Marking it lets someone who
            # registered but never opened the email sign in with a password
            # afterwards, and stops us mailing codes for an address already
            # proven.
            if not user.profile.email_verified:
                user.profile.email_verified = True
                user.profile.save(update_fields=['email_verified', 'updated_at'])
            return user, False

        user = User.objects.create_user(
            username=f'google-{claims["sub"]}'[:150],
            email=email,
            first_name=claims.get('given_name', ''),
            last_name=claims.get('family_name', ''),
        )
        user.profile.display_name = claims.get('name', '')
        # Same reasoning, and load-bearing here: create_user leaves the flag
        # False, which would leave every Google account permanently unverified
        # - unable to use password login later, and a standing target for
        # verification codes it can never need.
        user.profile.email_verified = True
        user.profile.save(
            update_fields=['display_name', 'email_verified', 'updated_at']
        )
        return user, True

    @action(
        detail=False,
        methods=['post'],
        url_path='verify-email',
        url_name='verify-email',
        permission_classes=[AllowAny],
    )
    def verify_email(self, request):
        """Confirm the emailed code, activate the account and sign in.

        Signing in here is what makes the round trip worth it: the user has
        just proved they own the address and typed a secret, so asking for
        the password again would be pure friction.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        data = {
            'user': UserSerializer(user, context=self.get_serializer_context()).data,
            'tokens': issue_tokens(user),
        }
        return Response(data)

    @action(
        detail=False,
        methods=['post'],
        url_path='resend-code',
        url_name='resend-code',
        permission_classes=[AllowAny],
    )
    def resend_code(self, request):
        """Issue a fresh code, replacing any code still outstanding."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        otp = serializer.save()
        return Response(verification_payload(otp))

    # -- sessions ---------------------------------------------------------

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def login(self, request):
        """Exchange credentials for a token pair.

        The `username` field accepts a username or an email address.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data)

    @action(
        detail=False,
        methods=['post'],
        url_path='token/refresh',
        url_name='token-refresh',
        permission_classes=[AllowAny],
    )
    def refresh(self, request):
        """Trade a refresh token for a new pair.

        Rotation is on: the token sent here is blacklisted and a new refresh
        token comes back with the response, so clients must store both.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Nested under `tokens` to match login and register.
        return Response({'tokens': serializer.validated_data})

    @action(detail=False, methods=['post'])
    def logout(self, request):
        """Blacklist the caller's refresh token."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(None)

    # -- passwords ----------------------------------------------------------

    @action(
        detail=False,
        methods=['post'],
        url_path='forgot-password',
        url_name='forgot-password',
        permission_classes=[AllowAny],
    )
    def forgot_password(self, request):
        """Email a password reset link, if the address has an account.

        Answers the same way whether or not the address is registered - see
        ForgotPasswordSerializer - so this cannot be used to enumerate
        accounts.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(None)

    @action(
        detail=False,
        methods=['post'],
        url_path='reset-password',
        url_name='reset-password',
        permission_classes=[AllowAny],
    )
    def reset_password(self, request):
        """Consume a reset link's uid/token and set a new password."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(None)

    @action(
        detail=False,
        methods=['post'],
        url_path='change-password',
        url_name='change-password',
    )
    def change_password(self, request):
        """Change the signed-in user's password.

        No AllowAny override here: this action keeps the viewset's default
        IsAuthenticated, since it operates on request.user.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(None)

    # -- the signed-in user ----------------------------------------------

    @action(detail=False, methods=['get', 'patch'])
    def me(self, request):
        """Read or update the signed-in account."""
        return self._retrieve_or_update(request, request.user)

    @action(
        detail=False,
        methods=['get', 'patch'],
        url_path='me/profile',
        url_name='me-profile',
        # Multipart so the avatar can be sent as a file alongside the other
        # fields; JSON when there is no file.
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def profile(self, request):
        """Read or update the signed-in user's profile."""
        # get_or_create rather than request.user.profile: an account made
        # before the signal existed would otherwise 500 here.
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return self._retrieve_or_update(request, profile)

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Find people to message, by username prefix or exact phone number.

        Paginated, so a common prefix cannot be used to pull the whole user
        table down in one request.
        """
        matches = search_users(request.query_params.get('q'), exclude=request.user)

        page = self.paginate_queryset(matches)
        serializer = PublicUserSerializer(
            page if page is not None else matches,
            many=True,
            context=self.get_serializer_context(),
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='me/blocks', url_name='me-blocks')
    def blocks(self, request):
        """Everybody I have blocked.

        Mine alone, and only ever mine: nothing anywhere tells one person that
        another has blocked them, which is why there is no route that reads
        somebody else's list.
        """
        rows = (
            UserBlock.objects.filter(blocker=request.user)
            .select_related('blocked_user__profile')
        )
        return Response(BlockedEntrySerializer(rows, many=True).data)

    @action(detail=False, methods=['post'], url_path='me/blocks/add', url_name='me-block')
    def block(self, request):
        """Stop direct contact with one person.

        Stops direct messages both ways, stops either of us adding the other to
        a group, and takes them out of my search results. It does NOT touch any
        group we already share: we both stay members, we both still see each
        other's messages there, and neither of us is told anything about the
        other's membership changing.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        blocks.block(
            request.user,
            user=serializer.validated_data['target_user'],
            phone_number=serializer.validated_data['target_number'],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'], url_path='me/blocks/remove',
            url_name='me-unblock')
    def unblock(self, request):
        """Undo it. Answers the same whether or not there was anything to undo."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        blocks.unblock(
            request.user,
            user=serializer.validated_data['target_user'],
            phone_number=serializer.validated_data['target_number'],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _retrieve_or_update(self, request, instance):
        """GET and PATCH against one object, shared by `me` and `profile`."""
        if request.method == 'GET':
            return Response(self.get_serializer(instance).data)

        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
