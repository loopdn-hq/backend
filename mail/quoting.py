"""Finding the part of a reply the sender actually wrote.

An email reply carries the whole thread under it. Posting that into a group
would put the last message back a second time, with a header, under every
reply - and the third reply would carry the second and the first. Two exchanges
in and the group is unreadable.

THIS IS A HEURISTIC AND ADMITS IT
---------------------------------
There is no marker in the standards that says "the quoted part starts here".
Every mail client invents its own, they change between versions, and they are
localised. So this recognises the common ones and stops at the first, and when
it recognises nothing it returns the text unchanged - which is the safe
failure: a reply with some quoted text under it is untidy, and a reply cut at
the wrong line has lost what somebody wrote.

Pure: a string in, a string out. No Django, no models, so the cases below can
be tested exhaustively and cheaply, which is the only way this stays correct as
clients are added.
"""
import re

# Each of these marks the start of quoted material. Anchored to the start of a
# line, because every one of them is also a phrase somebody could write in the
# middle of a sentence.
_SEPARATORS = [
    # Gmail, Apple Mail, and most clients that follow them. The date in the
    # middle varies wildly, so this matches the ends and not the contents. It
    # is also frequently wrapped across two lines by the sending client, which
    # is why the newline is allowed inside.
    re.compile(r'^\s*On .{0,200}?\s*wrote:\s*$', re.MULTILINE | re.DOTALL),
    # Outlook, English and the separator it uses in most locales.
    re.compile(r'^\s*-{2,}\s*Original Message\s*-{2,}\s*$', re.MULTILINE | re.I),
    re.compile(r'^\s*_{10,}\s*$', re.MULTILINE),
    # Outlook again: the header block it writes above the quote. `From:` on its
    # own is NOT enough - somebody writing "From: the supplier" in a sentence
    # would have the rest of their message cut off - so this requires a second
    # header line directly under it, which is what makes it a block rather than
    # a line of prose.
    re.compile(
        r'^\s*From:\s*.+\n(?:\s*(?:Sent|To|Cc|Date|Subject):\s*.*\n?)+',
        re.MULTILINE,
    ),
    # Our own footer, if a client quotes it back rather than the whole body.
    # The real defence is that the footer opens with `-- ` (RFC 3676), which
    # _SIGNATURE below already cuts at; these catch a client that reflowed it
    # and lost the marker.
    re.compile(r'^\s*Reply to this email to respond to the group\.\s*$', re.MULTILINE),
    re.compile(r'^\s*You are getting this because you are in .{0,200}$', re.MULTILINE),
]

# `-- ` on its own line, the one convention that IS standard (RFC 3676 §4.3).
_SIGNATURE = re.compile(r'^-- \s*$', re.MULTILINE)

# One line at a time, for the bottom-posting fallback below. The multi-line
# patterns above cannot be applied to a single line, and the point here is
# different anyway: not "where does the quote begin" but "is this line part of
# one". Header lines are listed individually because in this direction there
# is no block to anchor them to.
_QUOTE_LINE = re.compile(
    r'^\s*(?:'
    r'-- \s*'
    r'|On .{0,200}?wrote:\s*'
    r'|-{2,}\s*Original Message\s*-{2,}\s*'
    r'|_{10,}\s*'
    r'|(?:From|Sent|To|Cc|Date|Subject):\s*.*'
    r'|Reply to this email to respond to the group\.\s*'
    r'|You are getting this because you are in .*'
    r'|To stop emails from this group,.*'
    r'|Or use this link:.*'
    r'|Other groups are not affected\.\s*'
    r')$',
    re.IGNORECASE,
)


def strip_quoted_reply(text):
    """The sender's own words, with the quoted thread and signature removed."""
    body = (text or '').replace('\r\n', '\n').replace('\r', '\n')

    head = _above_the_quote(body)
    if head:
        return head

    # Nothing above the quote. Either the message really was only a quote - an
    # auto-reply, or somebody who hit send on an empty draft - or they BOTTOM
    # POSTED, writing underneath the thread the way Outlook and most of the
    # world outside Gmail still do.
    #
    # Cutting at the first separator returns an empty string for both, and
    # treating a bottom-posted reply as empty discards a real message in
    # silence. So when the top is empty, take everything that is not itself
    # quoted instead - which keeps what they wrote wherever they put it.
    return _below_the_quote(body)


def _above_the_quote(body):
    """Everything before the first separator, trimmed of trailing quote lines."""
    cut = len(body)
    for pattern in _SEPARATORS:
        match = pattern.search(body)
        if match is not None:
            cut = min(cut, match.start())

    signature = _SIGNATURE.search(body)
    if signature is not None:
        cut = min(cut, signature.start())

    # Whatever survived may still end in the `> ...` lines that led up to a
    # separator we did not recognise. Trailing only: a quote in the MIDDLE is
    # somebody replying inline, point by point, and dropping that would throw
    # away the half of the message they wrote.
    lines = body[:cut].split('\n')
    while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith('>')):
        lines.pop()

    return '\n'.join(lines).strip()


def _below_the_quote(body):
    """What is left once every quoted and separator line is dropped.

    The fallback, not the path: it loses inline replies, which is why it is
    only reached when reading the message the ordinary way found nothing at
    all.
    """
    kept = []
    for line in body.split('\n'):
        if line.lstrip().startswith('>'):
            continue
        if _QUOTE_LINE.match(line):
            continue
        kept.append(line)

    return '\n'.join(kept).strip()
