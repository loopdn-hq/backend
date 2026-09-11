"""Whether the From address on an inbound email can be believed.

WHY THIS EXISTS
---------------
`mail/inbound.py` accepts a reply only when the From address is the member's
own verified address. That check is worth exactly as much as the From header
is hard to forge - which, on its own, is not at all: anybody can type any From.
What makes it hard is SPF, DKIM and DMARC, checked by the mail system that
received the message, which writes its verdict into an `Authentication-Results`
header (RFC 8601). This module reads that header and says whether the From can
be trusted. It decides nothing else and touches no database.

THE RULE
--------
The From address is trusted when the receiving server says any one of:

    dmarc=pass for the From domain               DMARC is alignment by definition,
                                                  so the server's own record of
                                                  which From it evaluated must
                                                  name ours
    dkim=pass  and the signing domain is aligned   somebody holding that domain's
               with the From domain                key signed this exact message
    spf=pass   and the envelope sender is aligned  the sending server is one the
               with the From domain                From domain authorises

"Aligned" is DMARC's relaxed sense: the same domain, or one a subdomain of the
other. `bounce.example.com` may speak for `example.com`; `example.com.evil.net`
may not.

Anything else - a failing verdict, a passing DKIM for somebody else's domain,
no header at all - is not trusted. Absent is not trusted because the
alternative is a switch anybody can flip by stripping a header, and because
whether the provider writes this header at all is exactly the thing a live
account has to confirm before replies are turned on. Fail closed until it does.

ONLY THE FIRST HEADER IS READ
-----------------------------
A message may carry several Authentication-Results headers: the receiving
server prepends its own, and anything already there was written by a server
before it - or by the sender, who can write whatever they like. RFC 8601 tells
a receiver to strip headers that claim to be its own; whether ours does is not
something to bet a group's privacy on. So the newest header, the first, is the
only one read.
"""
from dataclasses import dataclass
from email.utils import parseaddr
import re

PASS = 'pass'
HEADER = 'authentication-results'

_COMMENT = re.compile(r'\([^()]*\)')


@dataclass(frozen=True)
class Verdict:
    """What the receiving server concluded, reduced to the three that matter."""

    dmarc: str = 'none'
    dmarc_domain: str = ''
    dkim: str = 'none'
    dkim_domain: str = ''
    spf: str = 'none'
    spf_domain: str = ''

    def trusts(self, from_address):
        """Whether this verdict vouches for that From address."""
        domain = _from_domain(from_address)
        if not domain:
            return False
        if self.dmarc == PASS and (
            not self.dmarc_domain or _aligned(self.dmarc_domain, domain)
        ):
            # `header.from` is what the server evaluated. A pass for some other
            # From than the one in front of us vouches for nothing here.
            return True
        if self.dkim == PASS and _aligned(self.dkim_domain, domain):
            return True
        if self.spf == PASS and _aligned(self.spf_domain, domain):
            return True
        return False


def from_is_trusted(headers, from_address):
    """The question `mail/inbound.py` asks. Headers are keyed lower-case."""
    return verdict_from(headers).trusts(from_address)


def verdict_from(headers):
    """Parse the newest Authentication-Results header into a Verdict.

    The header reads `authserv-id; method=result prop=value ...; ...`, with
    free-text comments in parentheses anywhere - `spf=pass (google.com: domain
    of x designates y as permitted sender) smtp.mailfrom=x`. Comments go first,
    then the segments, then each segment's leading `method=result`.

    Where a method appears more than once - two DKIM signatures, say - a pass
    is kept over anything else, because one valid signature is a valid
    signature however many others were broken in transit.
    """
    raw = _newest(headers)
    if not raw:
        return Verdict()

    results = {}
    for segment in _without_comments(raw).split(';')[1:]:
        tokens = segment.split()
        if not tokens or '=' not in tokens[0]:
            continue
        method, _, result = tokens[0].partition('=')
        method, result = method.strip().lower(), result.strip().lower()
        if results.get(method, ('', {}))[0] == PASS:
            continue
        results[method] = (result, _properties(tokens[1:]))

    dmarc, dmarc_props = results.get('dmarc', ('none', {}))
    dkim, dkim_props = results.get('dkim', ('none', {}))
    spf, spf_props = results.get('spf', ('none', {}))

    return Verdict(
        dmarc=dmarc,
        dmarc_domain=_domain_of(dmarc_props.get('header.from', '')),
        dkim=dkim,
        dkim_domain=(
            _domain_of(dkim_props.get('header.d', ''))
            or _domain_of(dkim_props.get('header.i', ''))
        ),
        spf=spf,
        spf_domain=_domain_of(spf_props.get('smtp.mailfrom', '')),
    )


def _newest(headers):
    value = (headers or {}).get(HEADER)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ''
    return str(value or '').strip()


def _without_comments(value):
    # Comments nest - `(a (b) c)` - so strip innermost first until none remain.
    while True:
        stripped = _COMMENT.sub(' ', value)
        if stripped == value:
            return value
        value = stripped


def _properties(tokens):
    properties = {}
    for token in tokens:
        key, _, value = token.partition('=')
        if value:
            properties[key.strip().lower()] = value.strip().strip('"')
    return properties


def _from_domain(from_address):
    """The domain of a From ADDRESS - which must be one, or there is nothing to vouch for."""
    _, address = parseaddr((from_address or '').strip())
    if '@' not in address:
        return ''
    return _domain_of(address)


def _domain_of(value):
    """`example.com` out of `Alice <a@Example.com>`, `a@example.com`, or a bare domain.

    For header PROPERTIES, which may legitimately be a bare domain
    (`header.d=example.com`) or an address (`smtp.mailfrom=bounce@example.com`).
    """
    value = (value or '').strip().strip('"')
    if not value:
        return ''
    if '@' in value or '<' in value:
        _, address = parseaddr(value)
        value = address or value
    return value.rpartition('@')[2].strip().lower().rstrip('.')


def _aligned(one, other):
    """DMARC relaxed alignment: equal, or one a subdomain of the other."""
    if not one or not other:
        return False
    return one == other or one.endswith('.' + other) or other.endswith('.' + one)
