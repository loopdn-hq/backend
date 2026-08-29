"""Route every failure through the same envelope.

Without this, DRF answers unhandled exceptions with Django's HTML 500 page,
so a client that always expects JSON blows up on the one response it most
needs to read. Here anything that escapes a view is logged with its full
traceback and returned as a normal `code: -1` payload.
"""
import logging

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from otp.models import OtpError

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    """DRF EXCEPTION_HANDLER: always return a renderable DRF Response."""
    # Django's own ValidationError (raised by model.full_clean() and by field
    # validators) is not something DRF knows about; translate it first.
    if isinstance(exc, DjangoValidationError):
        exc = _as_drf_validation_error(exc)

    # OtpError carries a message written for the user (expired code, resend
    # cooldown), so it is a 400 rather than a crash.
    if isinstance(exc, OtpError):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        exc = DRFValidationError({'detail': str(exc)})

    # TokenError is a plain Exception, so an expired or blacklisted refresh
    # token would otherwise fall through to the 500 branch below. This is the
    # same translation simplejwt's own views do.
    if isinstance(exc, TokenError):
        exc = InvalidToken(exc.args[0] if exc.args else 'Token is invalid or expired.')

    response = drf_exception_handler(exc, context)
    if response is not None:
        return response

    # Past this point DRF had no opinion, so the exception is a genuine bug or
    # a database-level rejection. Log it with the traceback either way.
    view = context.get('view')
    logger.exception('Unhandled exception in %s', view.__class__.__name__ if view else 'API')

    if isinstance(exc, IntegrityError):
        return Response(
            {'detail': 'That record conflicts with one that already exists.'},
            status=status.HTTP_409_CONFLICT,
        )

    # In DEBUG the exception text is far more useful than a generic line; in
    # production it would leak internals, so it is replaced.
    detail = f'{exc.__class__.__name__}: {exc}' if settings.DEBUG else 'Internal server error.'
    return Response({'detail': detail}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _as_drf_validation_error(exc):
    from rest_framework.exceptions import ValidationError as DRFValidationError

    if hasattr(exc, 'message_dict'):
        return DRFValidationError(exc.message_dict)
    return DRFValidationError(exc.messages)
