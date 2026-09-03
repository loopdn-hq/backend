"""Reading a text message to see whether it is a command.

Pure: a string in, a (command, argument) pair out. No database, no network, no
Django beyond nothing at all - which is what makes the rules here cheap to test
exhaustively, and they need to be.

THE RULE THAT MATTERS MOST
--------------------------
A command is the *whole message*, not a word inside it. `STOP` is a command;
`stop by the shop on your way home` is a sentence somebody meant to send to
their family. Matching on "contains STOP" would silently unsubscribe a member
and delete their group traffic, and they would have no idea why.

The single exception is JOIN, which takes an optional code after it - that is
the one command whose whole point is to carry an argument.

CASE AND PUNCTUATION
--------------------
Phone keyboards capitalise the first word, autocorrect adds full stops, and
people add exclamation marks. All of that is noise around the keyword, so the
body is upper-cased, stripped, and stripped again of trailing punctuation before
anything is compared.
"""

JOIN = 'join'
HELP = 'help'
MUTE = 'mute'
UNMUTE = 'unmute'
LEAVE = 'leave'
STOP = 'stop'
START = 'start'

# The carrier-mandated opt-out words. Every one of these has to work on every
# route, whatever the product would otherwise like them to mean - a carrier
# audit tests them literally.
_STOP_WORDS = {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT'}
# The matching opt-in words, same reason.
_START_WORDS = {'START', 'UNSTOP', 'YES'}
_HELP_WORDS = {'HELP', 'INFO'}

_SINGLE_WORD = {
    **{word: STOP for word in _STOP_WORDS},
    **{word: START for word in _START_WORDS},
    **{word: HELP for word in _HELP_WORDS},
    'MUTE': MUTE,
    'UNMUTE': UNMUTE,
    'LEAVE': LEAVE,
    'JOIN': JOIN,
}

# Trailing noise a phone adds on its own. Not stripped from the middle of a
# message - only from the ends, where it cannot change the words.
_TRIM = ' \t\r\n.!?,;:'


def parse(body):
    """Return (command, argument) for a command message, else (None, None).

    `argument` is only ever set for JOIN, and is the raw remainder of the
    message with its own surrounding whitespace removed.
    """
    if not body:
        return None, None

    cleaned = str(body).strip(_TRIM)
    if not cleaned:
        return None, None

    parts = cleaned.split(None, 1)
    first = parts[0].strip(_TRIM).upper()

    if first == 'JOIN':
        # `JOIN`, `JOIN FAMILY`, `JOIN family-2`. Everything after the keyword
        # is the code, because a code is one token and anything else is a typo
        # we would rather reject loudly than half-match.
        argument = parts[1].strip(_TRIM) if len(parts) > 1 else ''
        return JOIN, argument or None

    # Every other command must be the entire message.
    if len(parts) > 1:
        return None, None

    command = _SINGLE_WORD.get(first)
    if command is None:
        return None, None
    return command, None


def is_opt_out(command):
    return command == STOP


def is_opt_in(command):
    return command == START
