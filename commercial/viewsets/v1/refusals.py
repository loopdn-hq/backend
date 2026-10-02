"""What the campaign services refuse with, as API answers.

A service raises plain exceptions (commercial/services/errors.py) so it knows
nothing about HTTP; the views wrap their calls in `service_errors()`.
"""
from contextlib import contextmanager

from rest_framework.exceptions import APIException, NotFound, ValidationError

from commercial.models import Company
from commercial.services.errors import Invalid, Refused

COMPANY_NOT_FOUND_DETAIL = 'No company was found with that id.'


class Conflict(APIException):
    """Well formed, but not possible in the campaign's current state."""

    status_code = 409
    default_detail = "That isn't possible for this campaign now."
    default_code = 'conflict'


@contextmanager
def service_errors():
    try:
        yield
    except Invalid as exc:
        raise ValidationError(exc.errors)
    except Refused as exc:
        raise Conflict(exc.message)


def manageable_company(user, company_id):
    """One of the caller's companies, or a 404 - the same for "not yours" as for
    "no such company".
    """
    company = Company.objects.manageable_by(user).filter(pk=company_id).first()
    if company is None:
        raise NotFound(COMPANY_NOT_FOUND_DETAIL)
    return company
