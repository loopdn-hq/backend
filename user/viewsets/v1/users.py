"""Version 1 of the user API.

One viewset covers the whole account lifecycle:

    POST   /api/v1/users/register/        create an account (active; see settings)
    POST   /api/v1/users/verify-email/    confirm the code -> account activated
    POST   /api/v1/users/resend-code/     issue a fresh verification code
    POST   /api/v1/users/login/           username-or-email + password -> tokens
    POST   /api/v1/users/logout/          blacklist the caller's refresh token
    POST   /api/v1/users/token/refresh/   trade a refresh token for a new pair
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
from django.conf import settings
from django.contrib.auth.models import User
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from otp.services import start_email_verification
from user.lookups import search_users
from user.models import Profile
from user.serializers import (
    LoginSerializer,
    LogoutSerializer,
    ProfileSerializer,
    PublicUserSerializer,
    RegisterSerializer,
    ResendCodeSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)


def issue_tokens(user):
    """The access/refresh pair, in the shape every auth action returns."""
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


def verification_payload(otp):
    """What the client needs to drive the "enter your code" screen."""
    payload = {
        'email': otp.profile.user.email,
        'expires_at': otp.expiry,
        'resend_available_in': otp.seconds_until_resend,
    }

    # Nothing delivers the code yet, so development returns it directly.
    # Guarded on DEBUG: in production this line would hand the account to
    # anyone who can call the endpoint with a known email address.
    if settings.DEBUG:
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
        'verify_email': VerifyEmailSerializer,
        'resend_code': ResendCodeSerializer,
        'login': LoginSerializer,
        'logout': LogoutSerializer,
        'refresh': TokenRefreshSerializer,
        'me': UserSerializer,
        'profile': ProfileSerializer,
    }

    success_messages = {
        'register': 'Account created. Enter the verification code to activate it.',
        'verify_email': 'Email verified. Your account is now active.',
        'resend_code': 'A new verification code has been issued.',
        'login': 'Login successful.',
        'logout': 'Logged out.',
        'refresh': 'Token refreshed.',
        'me:GET': 'Account fetched.',
        'me:PATCH': 'Account updated.',
        'profile:GET': 'Profile fetched.',
        'profile:PATCH': 'Profile updated.',
        'retrieve': 'User fetched.',
        'search': 'Search complete.',
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

        With REQUIRE_EMAIL_VERIFICATION off (the current setting) the account
        is active immediately, so the caller can log in right away. With it
        on, the account is created inactive and a verification code is
        issued instead.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        data = {'user': UserSerializer(user, context=self.get_serializer_context()).data}

        if settings.REQUIRE_EMAIL_VERIFICATION:
            data['verification'] = verification_payload(start_email_verification(user))
            message = 'Account created. Enter the verification code to activate it.'
        else:
            message = 'Account created successfully. You can now log in.'

        response = Response(data, status=status.HTTP_201_CREATED)
        # The renderer prefers a message set on the response over the one in
        # success_messages, which cannot vary per request.
        response.message = message
        return response

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

    def _retrieve_or_update(self, request, instance):
        """GET and PATCH against one object, shared by `me` and `profile`."""
        if request.method == 'GET':
            return Response(self.get_serializer(instance).data)

        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
