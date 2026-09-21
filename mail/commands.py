"""Reading an email reply to see whether it is a command.

Pure: a string in, a command out. No database, no network, no Django - which is
what makes the rules cheap to test exhaustively, and they need to be. The SMS
side learned that the expensive way.

THE RULE THAT MATTERS MOST, AGAIN
---------------------------------
A command is the *whole message*, not a word inside it. `sms/commands.py` says
this about STOP; it is just as true here. "Please stop emails about the party"
is a sentence somebody meant to send to their family, and reading it as a
command would swallow the message and silently unsubscribe them - which is
exactly the bug that had to be fixed on the SMS side after a conversational
"No" started doing both.

WHY TWO WORDS
-------------
`STOP EMAILS` rather than `NO`, `STOP` or `UNSUBSCRIBE`, and the length is the
safety.

  - `NO` and `STOP` are plausible entire messages to a group. They needed a
    "only while a question is open" gate on the SMS side; a two-word imperative
    needs none, because there is no context in which somebody's whole reply is
    those two words and they mean something else.

    That gate is exactly what `parse_consent` at the foot of this module is
    for. It reads `YES` and `NO`, and `mail/inbound.py` honours them ONLY while
    consent is still PENDING - so the rule here is unchanged: a single word is
    unsafe unconditionally, which is why STOP EMAILS is two.
  - `UNSUBSCRIBE` reads as "block everything from this sender", which is the
    opposite of what this does. It stops ONE group.

THE FOOTER IS QUOTED BACK, AND THAT IS HANDLED ELSEWHERE
--------------------------------------------------------
Every group email prints this keyword, so most replies carry it in the quoted
thread underneath. Two things stop that being read as a command: the footer is
introduced by `-- ` on its own line, which is RFC 3676's signature marker and
is what every quote-stripper in the world already cuts at; and `mail/inbound.py`
runs `strip_quoted_reply` BEFORE asking this module anything. The ordering is
not incidental - see that module.
"""

STOP_EMAILS = 'stop_emails'

# The one string printed in every group email. Templates take it from here
# rather than spelling it out, because two copies of a keyword is two places to
# drift and one of them is the copy users read.
KEYWORD = 'STOP EMAILS'

# The plural is what we teach; the singular is what half the world will type.
_ALIASES = {'STOP EMAILS', 'STOP EMAIL'}

# Trailing noise a mail client or a person adds on its own. Stripped from the
# ends only, never the middle, where it could change the words.
_TRIM = ' \t\r\n.!?,;:'


def parse(body):
    """Return the command this message is, or None if it is a message.

    Whitespace inside is collapsed before comparing, so `STOP  EMAILS` and a
    line break between the two words both still count - a mail client is
    entitled to re-wrap what somebody typed.
    """
    if not body:
        return None

    cleaned = ' '.join(str(body).strip(_TRIM).split()).upper()
    if cleaned in _ALIASES:
        return STOP_EMAILS
    return None


def is_stop_emails(body):
    return parse(body) == STOP_EMAILS


# --- answering the consent question ---------------------------------------
#
# YES and NO, and kept OUT of `parse` on purpose: everything above is about a
# command that must work at ANY time, and these two are answers that only mean
# anything while a question is open. Mixing them would make "STOP EMAILS is the
# whole message" and "YES is the whole message" look like the same rule, and
# they are not - one is unconditional, the other is gated on consent still
# being PENDING by `mail/inbound.py`.
#
# WHY THE STRICTNESS. Exactly `YES` or exactly `NO`, upper-cased and stripped
# of trailing punctuation, and nothing else. Not `Y`, not `OK`, not `Sure`, not
# first-word matching on "yes please". `sms/commands.py` already wrote the
# reason and it is the same reason here: a guess here is a guess about consent,
# and reading "Y" as agreement is how somebody who never really answered starts
# being emailed. The mitigation for "yes please" is the copy in
# `bridge_consent` - *reply with just the word YES* - and one nudge when
# somebody does it anyway, not a looser parser.

YES = 'yes'
NO = 'no'

_CONSENT_WORDS = {'YES': YES, 'NO': NO}


def parse_consent(body):
    """Return YES, NO, or None if this reply is neither.

    Caller decides whether an answer means anything - see `mail/inbound.py`.
    Once consent has been given this is never reached, which is what lets
    somebody answer "no" to an ordinary question without unsubscribing
    themselves.
    """
    if not body:
        return None

    cleaned = ' '.join(str(body).strip(_TRIM).split()).upper()
    return _CONSENT_WORDS.get(cleaned)
