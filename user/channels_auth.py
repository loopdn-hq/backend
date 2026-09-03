"""JWT authentication for websocket connections.

DRF's authentication classes never run for a websocket - there is no DRF
request - so the handshake has to be authenticated here instead.

The browser WebSocket API cannot set headers, which is why the token is read
from the query string. That does mean it can land in server access logs, so
the Authorization header is preferred when the client can send one (native
apps can).
"""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


@database_sync_to_async
def user_from_token(raw_token):
    authenticator = JWTAuthentication()
    try:
        validated = authenticator.get_validated_token(raw_token)
        return authenticator.get_user(validated)
    except (InvalidToken, TokenError, AuthenticationFailed, KeyError):
        # AuthenticationFailed is DRF's, and is the PARENT of InvalidToken
        # rather than a subclass, so it does not arrive here by way of the
        # first entry. get_user raises it directly for a token whose user has
        # since been deactivated ('User is inactive') or deleted ('User not
        # found') - both perfectly ordinary once accounts can be banned. Uncaught
        # it escapes the middleware and kills the ASGI call before the consumer
        # runs, so the client sees a dropped connection instead of a clean 4401.
        return AnonymousUser()


def token_from_scope(scope):
    for name, value in scope.get('headers', []):
        if name == b'authorization':
            parts = value.decode(errors='ignore').split()
            if len(parts) == 2 and parts[0].lower() == 'bearer':
                return parts[1]

    query = parse_qs(scope.get('query_string', b'').decode(errors='ignore'))
    tokens = query.get('token') or []
    return tokens[0] if tokens else None


class JWTAuthMiddleware(BaseMiddleware):
    """Puts a User (or AnonymousUser) on scope['user'] before the consumer."""

    async def __call__(self, scope, receive, send):
        raw_token = token_from_scope(scope)
        scope['user'] = await user_from_token(raw_token) if raw_token else AnonymousUser()
        return await super().__call__(scope, receive, send)
