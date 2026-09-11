"""What Resend posts to us, and how we get the rest of it.

THE ONLY RESEND-SHAPED CODE ON THE WEBHOOK SIDE
-----------------------------------------------
Three shapes, and this module is the only place that knows any of them:

    an inbound event   `email.received`, posted to mail/webhooks.py. Metadata
                       only - an email_id, who it was from, who it was to, the
                       subject. In Resend's own words: "Webhooks do not include
                       the email body, headers, or attachments, only their
                       metadata."
    its content        GET /emails/receiving/{email_id}, which returns the
                       text, the html and the headers - and the headers are
                       where the SPF/DKIM/DMARC verdict lives, see
                       mail/authentication.py.
    a receipt          `email.delivered`, `.bounced`, `.complained`, `.failed`,
                       `.suppressed` - what happened to mail WE sent, keyed on
                       the id the send API returned. Acted on by
                       mail/receipts.py.

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

#: The kinds of event we act on, in our own words. Everything else the
#: provider can post - opened, clicked, scheduled - the webhook answers 200
#: and ignores.
INBOUND = 'inbound'
DELIVERED = 'delivered'
BOUNCED = 'bounced'
COMPLAINED = 'complained'
FAILED = 'failed'
SUPPRESSED = 'suppressed'

_EVENT_KINDS = {
    'email.received': INBOUND,
    'email.delivered': DELIVERED,
    'email.bounced': BOUNCED,
    'email.complained': COMPLAINED,
    'email.failed': FAILED,
    'email.suppressed': SUPPRESSED,
}


@dataclass(frozen=True)
class ProviderEvent:
    """One webhook event, reduced to what the rest of the app needs.

    `kind` is one of the constants above, or '' for an event that means
    nothing to us. `email_id` is the inbound email to fetch for INBOUND, and
    the id our own send was given for everything else. `detail` and
    `permanent` carry the provider's explanation of a bounce or failure for
    the delivery log; nothing here interprets them.
    """

    kind: str
    email_id: str
    to_address: str = ''
    from_address: str = ''
    detail: str = ''
    permanent: bool = False


@dataclass(frozen=True)
class ReceivedEmail:
    """One inbound email, fetched in full. Header names are lower-cased."""

    email_id: str
    from_address: str
    to_address: str
    subject: str = ''
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


def parse_event(payload):
    """One webhook payload -> a ProviderEvent. Never raises on shape."""
    kind = _EVENT_KINDS.get(payload.get('type') or '', '')
    data = payload.get('data') or {}
    if not isinstance(data, dict):
        data = {}

    if not kind:
        return ProviderEvent(kind='', email_id='')

    detail, permanent = '', False
    if kind == BOUNCED:
        bounce = data.get('bounce') or {}
        kind_of = str(bounce.get('type') or '').strip()
        permanent = kind_of.lower() == 'permanent'
        detail = ' '.join(
            part for part in (
                kind_of, str(bounce.get('subType') or '').strip(),
                str(bounce.get('message') or '').strip(),
            ) if part
        )
    elif kind == FAILED:
        detail = str((data.get('failed') or {}).get('reason') or '').strip()
    elif kind == SUPPRESSED:
        suppressed = data.get('suppressed') or {}
        detail = ' '.join(
            part for part in (
                str(suppressed.get('type') or '').strip(),
                str(suppressed.get('message') or '').strip(),
            ) if part
        )

    return ProviderEvent(
        kind=kind,
        email_id=str(data.get('email_id') or data.get('id') or '').strip(),
        to_address=_our_address(data),
        from_address=_first(data.get('from')),
        detail=detail,
        permanent=permanent,
    )


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
        subject=str(data.get('subject') or ''),
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
