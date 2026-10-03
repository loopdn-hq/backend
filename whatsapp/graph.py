"""Talking to Meta's Graph API: the WhatsApp Cloud API and Embedded Signup.

The one module that knows URLs, versions and payload shapes. Everything above it
passes plain arguments and gets plain dictionaries back, so `_request` is the
single seam every test patches - nothing in the suite reaches the network.

TWO KINDS OF TOKEN
------------------
    app credentials   WHATSAPP_APP_ID / WHATSAPP_APP_SECRET. Ours. Used to
                      exchange an Embedded Signup code and to inspect the token
                      that comes back.
    business token    the customer's, from that exchange. Used for everything
                      done ON their WABA: subscribing our app to it, registering
                      the number, sending messages.

Never log either, and never log a request body that carries one.

UNCONFIGURED MEANS LOG, NOT FAIL
--------------------------------
As in sms/client.py: with no app configured, `send_*` logs what it would have
sent and returns a synthetic id, so the fan-out runs end to end on a laptop and
in CI. Onboarding, by contrast, cannot be faked - there is no business on the
other side - so it raises.

Pure transport: settings and the standard library. No models.
"""
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


class WhatsAppError(Exception):
    """Meta refused or could not be reached. The message is safe to show.

    `code` is Meta's numeric error code when there was one, which is how a
    caller tells "this number is not on WhatsApp" (131026) from "the token was
    revoked" (190) from "try again" (131000 / timeouts).
    """

    def __init__(self, message, *, code=None, permanent=False):
        super().__init__(message)
        self.code = code
        # True when retrying will not help and the connection itself is broken.
        self.permanent = permanent


# Meta error codes that mean the CONNECTION is dead, not this one message.
# 190: token invalid/expired/revoked. 200-299: permission removed.
# 133010: phone number not registered.
_CONNECTION_BROKEN = {190, 133010}


def is_configured():
    return bool(settings.WHATSAPP_APP_ID and settings.WHATSAPP_APP_SECRET)


def _url(path, query=None):
    base = f'https://graph.facebook.com/{settings.WHATSAPP_GRAPH_VERSION}/{path.lstrip("/")}'
    if query:
        base += '?' + urllib.parse.urlencode(query)
    return base


def _request(method, url, *, token=None, payload=None, failure_message):
    """One HTTP call to the Graph API. Returns the decoded body."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if data is not None:
        headers['Content-Type'] = 'application/json'

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=settings.WHATSAPP_REQUEST_TIMEOUT
        ) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        code, detail = _read_error(exc)
        # The path only, never the query: an access-token exchange carries the
        # app secret and the code in its query string.
        logger.warning(
            'Graph API %s %s returned %s (code %s): %s',
            method, urllib.parse.urlsplit(url).path, exc.code, code, detail,
        )
        raise WhatsAppError(
            failure_message,
            code=code,
            permanent=code in _CONNECTION_BROKEN or 200 <= (code or 0) < 300,
        )
    except Exception as exc:
        logger.error(
            'Graph API unreachable at %s %s: %s',
            method, urllib.parse.urlsplit(url).path, exc,
        )
        raise WhatsAppError(failure_message)


def _read_error(exc):
    try:
        body = json.loads(exc.read().decode(errors='replace') or '{}')
    except ValueError:
        return None, ''
    error = body.get('error') or {}
    code = error.get('code')
    return (code if isinstance(code, int) else None), str(error.get('message', ''))[:300]


# --- Embedded Signup ---------------------------------------------------------


def exchange_code(code):
    """Turn the code from Embedded Signup into the business's token.

    Returns {'access_token', 'expires_in'}. Server side only: the exchange needs
    our app secret, which is why the browser hands us a code rather than a token.
    """
    if not is_configured():
        raise WhatsAppError('WhatsApp is not set up on this server yet.')

    data = _request(
        'GET',
        _url('oauth/access_token', {
            'client_id': settings.WHATSAPP_APP_ID,
            'client_secret': settings.WHATSAPP_APP_SECRET,
            'code': code,
        }),
        failure_message='We could not complete the WhatsApp connection. Please try again.',
    )
    token = (data or {}).get('access_token')
    if not token:
        raise WhatsAppError('We could not complete the WhatsApp connection. Please try again.')
    return {'access_token': token, 'expires_in': data.get('expires_in')}


def granted_waba_ids(token):
    """The WABA ids this token was actually granted, per Meta.

    THE OWNERSHIP CHECK. Embedded Signup reports the WABA and phone number to
    the browser, and the browser reports them to us - so both are claims. This
    asks Meta which accounts the token really covers, and onboarding refuses
    any WABA not in the answer. Without it, a tenant could post another
    tenant's WABA id alongside their own valid code and take over its inbound
    traffic.

    Returns None when Meta lists a WhatsApp scope with no `target_ids`, which is
    how it reports a grant that is not limited to particular assets. There is
    then no list to check against, and the caller relies on the next step -
    reading the WABA with this very token, which Meta refuses for an account
    the token does not cover.
    """
    app_token = f'{settings.WHATSAPP_APP_ID}|{settings.WHATSAPP_APP_SECRET}'
    data = _request(
        'GET',
        _url('debug_token', {'input_token': token}),
        token=app_token,
        failure_message='We could not verify the WhatsApp connection.',
    )
    info = (data or {}).get('data') or {}
    if not info.get('is_valid'):
        raise WhatsAppError('Meta did not accept that connection. Please try again.')

    granted = set()
    for scope in info.get('granular_scopes') or []:
        if scope.get('scope') in (
            'whatsapp_business_management',
            'whatsapp_business_messaging',
        ):
            if not scope.get('target_ids'):
                return None
            granted.update(str(t) for t in scope['target_ids'])
    return granted


def phone_numbers_of(waba_id, token):
    """The phone number ids that belong to this WABA."""
    data = _request(
        'GET',
        _url(f'{waba_id}/phone_numbers', {'fields': 'id'}),
        token=token,
        failure_message='We could not read the WhatsApp Business account.',
    )
    return {str(row.get('id')) for row in (data or {}).get('data') or []}


def subscribe_app(waba_id, token):
    """Ask Meta to send this WABA's webhooks to our app."""
    _request(
        'POST',
        _url(f'{waba_id}/subscribed_apps'),
        token=token,
        failure_message='We could not subscribe to the WhatsApp Business account.',
    )


def unsubscribe_app(waba_id, token):
    _request(
        'DELETE',
        _url(f'{waba_id}/subscribed_apps'),
        token=token,
        failure_message='We could not unsubscribe from the WhatsApp Business account.',
    )


def register_number(phone_number_id, token, pin=None):
    """Register the number for Cloud API messaging. Returns the PIN used.

    Registration requires a six-digit two-step verification PIN. A number that
    arrives from Embedded Signup has none set yet, so this one becomes it; a
    number that is already registered answers with an error we treat as success.
    """
    pin = pin or f'{secrets.randbelow(10**6):06d}'
    try:
        _request(
            'POST',
            _url(f'{phone_number_id}/register'),
            token=token,
            payload={'messaging_product': 'whatsapp', 'pin': pin},
            failure_message='We could not register the WhatsApp number.',
        )
    except WhatsAppError as exc:
        # Already registered (e.g. a reconnect) is fine; anything else is not.
        if exc.code not in (133005, 133015):
            raise
    return pin


def phone_number_details(phone_number_id, token):
    return _request(
        'GET',
        _url(phone_number_id, {
            'fields': 'display_phone_number,verified_name,quality_rating,'
                      'messaging_limit_tier,code_verification_status',
        }),
        token=token,
        failure_message='We could not read the WhatsApp number.',
    ) or {}


# --- Sending -------------------------------------------------------------------


def _send(phone_number_id, token, payload):
    if not is_configured():
        logger.info(
            'WhatsApp (not sent - no app configured) %s -> ...%s: %s',
            phone_number_id, str(payload.get('to', ''))[-4:], payload.get('type'),
        )
        return {'id': ''}

    data = _request(
        'POST',
        _url(f'{phone_number_id}/messages'),
        token=token,
        payload={'messaging_product': 'whatsapp', 'recipient_type': 'individual', **payload},
        failure_message='We could not send that WhatsApp message.',
    )
    messages = (data or {}).get('messages') or [{}]
    return {'id': messages[0].get('id') or ''}


def send_text(phone_number_id, token, *, to, text, reply_to=None):
    """Free-form text. Only valid inside the recipient's 24-hour window."""
    payload = {
        'to': to,
        'type': 'text',
        'text': {'body': text[:4096], 'preview_url': False},
    }
    if reply_to:
        payload['context'] = {'message_id': reply_to}
    return _send(phone_number_id, token, payload)


def send_template(phone_number_id, token, *, to, name, language, parameters):
    """An approved template. The only thing allowed outside the window."""
    return _send(phone_number_id, token, {
        'to': to,
        'type': 'template',
        'template': {
            'name': name,
            'language': {'code': language},
            'components': [{
                'type': 'body',
                'parameters': [{'type': 'text', 'text': p} for p in parameters],
            }],
        },
    })


def mark_read(phone_number_id, token, wamid):
    """Blue ticks on the sender's side. Best effort."""
    if not is_configured():
        return
    try:
        _request(
            'POST',
            _url(f'{phone_number_id}/messages'),
            token=token,
            payload={'messaging_product': 'whatsapp', 'status': 'read', 'message_id': wamid},
            failure_message='Could not mark as read.',
        )
    except WhatsAppError:
        pass
