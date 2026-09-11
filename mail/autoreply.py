"""Whether an inbound email was written by a machine.

A vacation responder answers our group email. Its From is the member's own
address, its DKIM passes, so every identity check says yes - and "I am out of
the office until Monday" is posted into the group, fanned back out to everyone,
and answered again by the next responder. Our sends carry `Auto-Submitted:
auto-generated` (RFC 3834) so a well-behaved responder stays quiet; this is
the other half, for the ones that do not.

Read from the headers the responder itself sets, which is the only reliable
signal there is:

    Auto-Submitted             RFC 3834. Anything but `no` is a machine.
    X-Autoreply, X-Autorespond older, still common
    X-Auto-Response-Suppress   Exchange sets it on out-of-office replies
    Precedence                 `bulk`, `junk` or `auto_reply`

and, as a last resort, a subject line that says so - "Automatic reply:",
"Out of Office" - because some responders set no header at all.

Errs towards accepting. A false positive here silently drops a real reply,
which is the worse failure, so nothing below guesses from the body.
"""
import re

#: Any value other than `no`, per RFC 3834 - `auto-generated`, `auto-replied`,
#: `auto-notified`, and vendor extensions after them.
AUTO_SUBMITTED = 'auto-submitted'

_PRESENCE_MEANS_AUTOMATIC = ('x-autoreply', 'x-autorespond', 'x-auto-response-suppress')
_AUTOMATIC_PRECEDENCE = frozenset({'bulk', 'junk', 'auto_reply'})

_AUTOMATIC_SUBJECT = re.compile(
    r'^\s*(?:(?:re|aw|fwd?)\s*:\s*)*'
    r'(?:automatic reply|auto(?:matic)?[\s-]*reply|autoreply|out of (?:the )?office|ooo\b)',
    re.IGNORECASE,
)


def is_automatic(headers, subject=''):
    """Whether these headers (or this subject) mark the mail as machine-sent."""
    headers = headers or {}

    submitted = _first(headers.get(AUTO_SUBMITTED)).split(';')[0].strip().lower()
    if submitted and submitted != 'no':
        return True

    for name in _PRESENCE_MEANS_AUTOMATIC:
        if name in headers:
            return True

    precedence = _first(headers.get('precedence')).strip().lower()
    if precedence in _AUTOMATIC_PRECEDENCE:
        return True

    return bool(_AUTOMATIC_SUBJECT.match(subject or ''))


def _first(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ''
    return str(value or '')
