"""What Resend hands us when somebody replies, and how we get the rest of it.

THE ONLY RESEND-SHAPED INBOUND CODE
-----------------------------------
Two shapes, and this module is the only place that knows either:

    the event      `email.received`, posted to mail/webhooks.py. Metadata
                   only - an email_id, who it was from, who it was to, the
                   subject. In Resend's own words: "Webhooks do not include the
                   email body, headers, or attachments, only their metadata."
    the content    GET /emails/receiving/{email_id}, which returns the text,
                   the html and the headers - and the headers are where the
                   SPF/DKIM/DMARC verdict lives, see mail/authentication.py.

`mail/webhooks.py` used to read `data.text` off the event. Resend never sends
it, so that path would have dropped every real reply as empty and said nothing.

A change of provider is a change to this file. Nothing here decides anything;
it reads, shapes and hands over.
"""
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from html import unescape
from urllib.parse import quote

from django.conf import settings

from utils.mail import RESEND_API_ROOT, fetch_json

#: The event types that mean "somebody wrote to us". Everything else the
#: provider can post - receipts, bounces, complaints - the webhook answers 200
#: and ignores.
INBOUND_EVENT_TYPES = frozenset({'email.received'})


@dataclass(frozen=True)
class ReceivedEmail:
    """One inbound email, fetched in full. Header names are lower-cased."""

    email_id: str
    from_address: str
    to_address: str
    text: str = ''
    html: str = ''
    headers: dict = field(default_factory=dict)

    @property
    def body(self):
        """What the person wrote, as plain text.

        Some clients send HTML only. Better a rough plain-text rendering than
        posting markup into a group, and better still than dropping it.
        """
        if self.text:
            return self.text
        return _strip_html(self.html) if self.html else ''


def event_summary(payload):
    """(email_id, to, from) out of one webhook event.

    An empty email_id for anything that is not an inbound message - a delivery
    receipt, a bounce, an event type added after this was written - which the
    webhook treats as nothing to do.
    """
    if (payload.get('type') or '') not in INBOUND_EVENT_TYPES:
        return '', '', ''

    data = payload.get('data') or {}
    email_id = str(data.get('email_id') or data.get('id') or '').strip()
    return email_id, _our_address(data), _first(data.get('from'))


def fetch_received(email_id):
    """The content behind one event. Raises EmailError if Resend will not say."""
    data = fetch_json(
        f'{RESEND_API_ROOT}/emails/receiving/{quote(email_id, safe="")}',
        failure_message='Could not fetch the inbound email from the provider.',
    ) or {}

    return ReceivedEmail(
        email_id=email_id,
        from_address=_first(data.get('from')),
        to_address=_our_address(data),
        text=str(data.get('text') or ''),
        html=str(data.get('html') or ''),
        headers=_lower_keys(data.get('headers') or {}),
    )


def _our_address(data):
    """The one recipient that is on our reply domain.

    A reply-all puts a friend in To and us second; a forward puts us in
    `received_for`. The first address that is ours is the one the reply was
    meant for. When none is, the first To address is returned so the webhook's
    "not ours" branch has something concrete to say no to.
    """
    candidates = [
        *_all(data.get('to')), *_all(data.get('cc')), *_all(data.get('received_for'))
    ]
    domain = settings.MAIL_REPLY_DOMAIN
    if domain:
        for address in candidates:
            if _domain_of(address) == domain:
                return address
    return candidates[0] if candidates else ''


def _all(value):
    """Every address out of a field that may be a string, a list, or wrapped."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        value = [value]
    addresses = []
    for item in value:
        if isinstance(item, dict):
            # Some providers wrap each address as {'address': ..., 'name': ...}.
            item = item.get('address') or item.get('email') or ''
        item = str(item or '').strip()
        if item:
            addresses.append(item)
    return addresses


def _first(value):
    addresses = _all(value)
    return addresses[0] if addresses else ''


def _domain_of(address):
    _, bare = parseaddr(address or '')
    return bare.rpartition('@')[2].strip().lower()


def _lower_keys(headers):
    """Header names compared case-insensitively, so they are stored that way."""
    if not isinstance(headers, dict):
        return {}
    return {str(key).lower(): value for key, value in headers.items()}


def _strip_html(html):
    """A crude plain-text rendering of an HTML-only reply.

    Crude on purpose. The alternative is an HTML parser deciding what a group
    message says, and the text alternative is present on the overwhelming
    majority of real mail - this is the fallback, not the path.
    """
    text = re.sub(r'(?is)<(script|style).*?</\1>', ' ', html)
    text = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</tr>', '\n', text)
    text = re.sub(r'(?s)<[^>]+>', '', text)
    return unescape(text).strip()
