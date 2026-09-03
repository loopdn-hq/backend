"""Transactional email, via the Resend API.

Same convention as utils/sms.py's relationship to Telnyx: with
`RESEND_API_KEY` unset, `send_email` logs instead of sending, so a laptop and
the test suite need no account, no credentials and no network.
"""
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

RESEND_API_ROOT = 'https://api.resend.com'


class EmailError(Exception):
    """Delivery failed for a reason that is ours, not the user's.

    The message is safe to show. Deliberately not a DRF exception - this
    module has to stay usable from a management command or a Celery task,
    not just from a view.
    """


def send_email(to, subject, html):
    """Send one HTML email via Resend.

    Returns the decoded Resend response, or None when running unconfigured.
    Raises EmailError if Resend refused or could not be reached.
    """
    if not _is_configured():
        logger.info('Email (not sent - Resend not configured) to %s: %s\n%s', to, subject, html)
        return None

    data = _post(
        f'{RESEND_API_ROOT}/emails',
        {
            'from': settings.DEFAULT_FROM_EMAIL,
            'to': [to],
            'subject': subject,
            'html': html,
        },
        failure_message='We could not send the email. Try again in a moment.',
    )

    email_id = (data or {}).get('id')
    logger.info('Email sent to %s (resend id %s)', to, email_id)
    return data


def _is_configured():
    return bool(settings.RESEND_API_KEY)


def _post(url, payload, *, failure_message):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            'Authorization': f'Bearer {settings.RESEND_API_KEY}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=settings.EMAIL_REQUEST_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Resend puts a human-readable reason in the body; worth logging, but
        # not worth showing a user who can do nothing with it.
        detail = exc.read().decode(errors='replace')[:500]
        logger.warning('Resend returned %s for %s: %s', exc.code, url, detail)
        raise EmailError(failure_message)
    except Exception as exc:
        # Timeout, DNS, TLS - anything meaning the request never landed.
        logger.error('Resend unreachable at %s: %s', url, exc)
        raise EmailError(failure_message)
