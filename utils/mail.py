"""Transactional email, via the Resend HTTP API.

WHAT WE ARE DOING FOR EMAIL
---------------------------
Resend sends the message; we own the content. That split is the opposite of the
SMS arrangement in sms/verification.py, where Telnyx owns the code itself -
worth knowing before reading the two side by side.

Resend can hold templates for you: its send endpoint accepts `template` and
`variables` instead of a body, rendered from something authored in their
dashboard. We deliberately do not use that. A template that lives in a web UI
is not in the repository, cannot be reviewed in a pull request, cannot be
rendered in a test, and silently changes under a deployment that did not
mention it. So every template here is a Django template under
`templates/email/<slug>/`, rendered in this process, and Resend receives
finished `html` and `text`.

ADDING THE NEXT EMAIL
---------------------
Two steps, no changes to this module's logic:

  1. Add a directory `templates/email/<slug>/` holding `subject.txt`,
     `body.html` and `body.txt`.
  2. Declare it in the CATALOGUE below, naming the context keys it needs.

Then `send_templated_email(PASSWORD_RESET, to=..., context={...})`.

Declaring the context keys is the point of the catalogue. Django templates
render an unknown variable as an empty string, so a typo'd key does not raise -
it mails somebody a sentence with a hole in it. `context_keys` turns that into
a loud failure before a single byte reaches the provider.

CONFIGURATION
-------------
Keyed on the environment, the same way the database, channel layer, file
storage and SMS are: with `RESEND_API_KEY` unset, `send_email` renders the
message and logs it instead of sending, so a laptop and the test suite need no
account, no credentials and no network.
"""
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

RESEND_API_ROOT = 'https://api.resend.com'

# Cloudflare fronts api.resend.com and bans urllib's default
# `Python-urllib/3.13` outright. The reply is a 403 whose body is the string
# `error code: 1010` - Cloudflare's "browser banned" page, not a Resend error -
# so it survives every amount of fixing the payload, the key or the sending
# domain, and the JSON decode of the error body fails too. Any explicit
# User-Agent gets through. Telnyx has no such rule, which is why sms/client.py
# does not need one.
USER_AGENT = 'loopd-backend'

# Resend answers a send with {"id": "..."}; these are the statuses that mean
# "the request was fine, the service was not". Separated from the rest only so
# that a misconfiguration - an unverified sending domain, a revoked key - is
# logged as an error a human must fix, rather than as a blip. Both still raise.
_TRANSIENT_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


class EmailError(Exception):
    """Delivery failed for a reason that is ours, not the user's.

    The message is safe to show. Deliberately not a DRF exception - this module
    has to stay usable from a management command or a Celery task, not just
    from a view. loopd/exceptions.py turns it into a 503, exactly as it does
    for SmsError.
    """


@dataclass(frozen=True)
class EmailTemplate:
    """One kind of email: a directory of templates plus the context it needs.

    `slug` is both the registry name and the directory under templates/email/,
    so there is only ever one string to keep in step.
    """

    slug: str
    context_keys: tuple = field(default_factory=tuple)

    @property
    def subject_template(self):
        return f'email/{self.slug}/subject.txt'

    @property
    def html_template(self):
        return f'email/{self.slug}/body.html'

    @property
    def text_template(self):
        return f'email/{self.slug}/body.txt'


# --- the catalogue ---------------------------------------------------------
#
# Every email the product can send, in one place. One entry today; the shape is
# what makes the second and third cost a directory and a line.

VERIFICATION_CODE = EmailTemplate(
    slug='verification_code',
    context_keys=('code', 'expires_in_minutes'),
)

# Somebody with no account was added to a group by their address. The link is
# a convenience, not a credential: membership is granted by VERIFYING the
# address, so a forwarded invitation gives the reader nothing.
GROUP_INVITATION = EmailTemplate(
    slug='group_invitation',
    context_keys=('group_name', 'inviter_name', 'join_url'),
)

CATALOGUE = {
    template.slug: template
    for template in (
        VERIFICATION_CODE,
        GROUP_INVITATION,
    )
}


def send_templated_email(template, to, context=None, **kwargs):
    """Render `template` and send it. The function everything else should call.

    `template` is an EmailTemplate from the catalogue above, or its slug.
    `context` must contain at least the template's declared `context_keys`;
    anything extra is passed through to the templates untouched.

    Returns the Resend message id, or None when running unconfigured.
    Raises EmailError if a template is missing a variable, or if Resend refused
    or could not be reached.
    """
    if not isinstance(template, EmailTemplate):
        try:
            template = CATALOGUE[str(template)]
        except KeyError:
            # A slug that is not in the catalogue is a programming error, but
            # it would otherwise surface as a confusing TemplateDoesNotExist
            # three frames deeper.
            raise EmailError(f'No email template named {template!r}.')

    context = dict(context or {})

    missing = [key for key in template.context_keys if key not in context]
    if missing:
        raise EmailError(
            f'Email template {template.slug!r} is missing context: '
            f'{", ".join(sorted(missing))}.'
        )

    context.setdefault('product_name', settings.EMAIL_PRODUCT_NAME)
    context.setdefault('support_address', settings.EMAIL_SUPPORT_ADDRESS)

    # Collapsed to a single line: a newline that reaches a Subject header is
    # header injection, and a trailing one from the template file is certain.
    subject = ' '.join(render_to_string(template.subject_template, context).split())

    return send_email(
        to=to,
        subject=subject,
        html=render_to_string(template.html_template, context),
        text=render_to_string(template.text_template, context),
        **kwargs,
    )


def send_email(to, subject, html, text=None, *, reply_to=None, idempotency_key=None):
    """Hand a finished message to Resend.

    Prefer `send_templated_email`. This is the transport, exposed because a
    one-off - an operational notice from a management command - should not have
    to invent a template directory to exist.

    Returns the Resend message id, or None when running unconfigured.
    Raises EmailError if Resend refused or could not be reached.
    """
    recipients = [to] if isinstance(to, str) else list(to)
    recipients = [address.strip() for address in recipients if address and address.strip()]

    if not recipients:
        raise EmailError('No recipient address to send to.')

    for address in recipients:
        # A newline in an address is header injection - it lets whatever put
        # this address here append headers of its own, Bcc included.
        if '\n' in address or '\r' in address:
            raise EmailError('That email address is not valid.')

    if not _is_configured():
        logger.info(
            'Email (not sent - Resend not configured) to %s\nSubject: %s\n%s',
            ', '.join(recipients),
            subject,
            text or html,
        )
        return None

    payload = {
        'from': settings.DEFAULT_FROM_EMAIL,
        'to': recipients,
        'subject': subject,
        'html': html,
    }

    # Always send a plain-text part when there is one: some clients refuse to
    # display HTML, and a message with no text alternative scores worse with
    # spam filters - which for a verification code is the difference between
    # signing up and not.
    if text:
        payload['text'] = text
    if reply_to:
        payload['reply_to'] = reply_to

    data = _post(
        f'{RESEND_API_ROOT}/emails',
        payload,
        failure_message='We could not send that email. Try again in a moment.',
        idempotency_key=idempotency_key,
    )

    message_id = (data or {}).get('id')
    # The recipient is logged, the body never is: these carry credentials.
    logger.info('Email sent to %s (resend id %s)', ', '.join(recipients), message_id)
    return message_id


def _is_configured():
    return bool(settings.RESEND_API_KEY)


def _post(url, payload, *, failure_message, idempotency_key=None):
    """POST JSON to Resend and return the decoded body."""
    headers = {
        'Authorization': f'Bearer {settings.RESEND_API_KEY}',
        'Content-Type': 'application/json',
        # Not optional - see USER_AGENT above.
        'User-Agent': USER_AGENT,
    }

    # Resend honours this for 24 hours, so a retry of a request that actually
    # landed does not mail the same code twice.
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key[:256]

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=settings.EMAIL_REQUEST_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Resend explains itself in the body; worth logging, never worth showing
        # a user who can do nothing with "domain is not verified".
        detail = exc.read().decode(errors='replace')[:500]
        if exc.code in _TRANSIENT_STATUSES:
            logger.warning('Resend returned %s for %s: %s', exc.code, url, detail)
        else:
            # 401, 403, 422: a key, a sending domain or a payload is wrong, and
            # it will stay wrong until somebody changes it. Logged loudly
            # because retrying cannot fix it.
            logger.error('Resend rejected %s with %s: %s', url, exc.code, detail)
        raise EmailError(failure_message)
    except Exception as exc:
        # Timeout, DNS, TLS - anything meaning the request never landed.
        logger.error('Resend unreachable at %s: %s', url, exc)
        raise EmailError(failure_message)
