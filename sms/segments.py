"""How many text messages one message actually costs.

Pure: a string in, a (segments, encoding) pair out. No Django, no database, no
network - which is what makes it cheap to test exhaustively, and it needs to be,
because every answer here is money.

WHY THIS EXISTS
---------------
"One message" is not a billing unit. A message is cut into 160-character pieces
if every character is in the GSM-7 alphabet, and into 70-character pieces if even
one character is not. Each piece is billed separately, to every recipient.

So a single curly quote - the kind a phone inserts automatically - turns a
100-character group message from one billed segment into two, for everybody in
the group, forever. An em dash in a message template does the same to every
message the product ever sends. That is not a rounding error: it is the
difference between a bill and three bills.

Concatenated messages carry a header that steals space, which is why a two-part
message gets 153 (not 160) characters per part, and 67 (not 70) in UCS-2.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Any notion of who is being texted or what it costs in their country. This
returns segments; pricing belongs to whoever knows the rates.
"""

# The GSM 03.38 basic alphabet. Anything outside this and the extension table
# below forces the whole message into UCS-2.
_GSM_BASIC = set(
    '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?'
    '¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà'
)

# These exist in GSM-7 but only as an escape sequence, so each one costs TWO
# characters rather than one. A message of 90 square brackets is two segments.
_GSM_EXTENDED = set('^{}\\[~]|€')

GSM7 = 'gsm7'
UCS2 = 'ucs2'

# Single message, then per-part once a message is split and each part has to
# carry a concatenation header.
_LIMITS = {
    GSM7: (160, 153),
    UCS2: (70, 67),
}


def encoding_for(text):
    """GSM-7 if every character fits it, otherwise UCS-2."""
    for char in text or '':
        if char not in _GSM_BASIC and char not in _GSM_EXTENDED:
            return UCS2
    return GSM7


def count_segments(text):
    """Return (segments, encoding) for one outbound message body.

    An empty message is one segment: a provider still charges for delivering
    nothing, and reporting zero would quietly under-count every spend ceiling
    that trusts this number.
    """
    text = text or ''
    encoding = encoding_for(text)

    if encoding == GSM7:
        # Extension characters occupy two of the seven-bit slots each.
        units = sum(2 if char in _GSM_EXTENDED else 1 for char in text)
    else:
        # UTF-16 code units, not Python characters. An emoji is one character
        # to len() and two units on the wire, so counting characters here
        # under-reports every message with an emoji in it - which, in a family
        # group chat, is most of them.
        units = len(text.encode('utf-16-le')) // 2

    single, per_part = _LIMITS[encoding]
    if units <= single:
        return 1, encoding

    # Ceiling division, because a single leftover character is a whole segment.
    return (units + per_part - 1) // per_part, encoding


def is_gsm7(text):
    """True when a string costs the cheap encoding.

    Used to keep the fixed parts of message templates honest: one em dash in a
    template makes every message the product sends cost UCS-2 rates.
    """
    return encoding_for(text) == GSM7
