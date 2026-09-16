"""Who a message names.

WHY IDS AND NOT WORDS
---------------------
A mention is stored as a row pointing at the membership it names, never as a
pattern found in the text. Parsing `@alice` back out of the words cannot work
here for three separate reasons, and each of them is somebody not being told
about a message that named them:

    most members have no handle   a phone-only member is `display_label` and
                                  nothing else - two of them can be called
                                  "Mum", and neither is a username
    names contain spaces          "@Uncle Bob" has no end a parser can find
                                  without guessing
    names change                  a display name edited after the fact would
                                  silently un-mention somebody in old messages

So the composer sends the ids it offered the sender, and this module is the one
place that decides whether they are allowed to stand.

WHAT THE TEXT CARRIES
---------------------
Nothing. The words stay exactly as they were typed - "@Bob can you bring the
bread" - so the SMS and email copies of the message read as a person wrote
them, with no markup a phone would show raw. The app highlights a mention by
matching these rows against the text; every other channel just reads the
sentence.

ONE ENTRY POINT
---------------
`set_mentions` is called by `chat/services.py:create_message` and by the edit
route, so a mention stored by the REST API, by the websocket and by an edit is
stored the same way. Validation lives here rather than in the two serializers,
because a rule written twice is a rule that will be enforced once.
"""
import logging

from chat.models import MessageMention, Participant

logger = logging.getLogger(__name__)


def set_mentions(message, participant_ids=None, everyone=False):
    """Record who this message names, replacing whatever it named before.

    Returns the Participant rows that were really named - the ids that survived
    validation - so a caller can act on them without asking again.

    Replaces rather than adds, because an edit is the other caller: taking
    "@Bob" out of the words has to take Bob out of the mentions, or he keeps a
    highlight on a message that no longer names him.

    Refusals are silent. An id for somebody who has since left the group, an id
    from another conversation, or a duplicate is dropped and the rest are
    stored: a mention is a nicety on top of a message that is already written,
    and failing the send because one name went stale would be a worse answer
    than sending it with one fewer highlight.
    """
    rows = _allowed(message, participant_ids)

    MessageMention.objects.filter(message=message).delete()
    if rows:
        MessageMention.objects.bulk_create(
            [
                MessageMention(message=message, participant=row)
                for row in rows
            ]
        )

    everyone = bool(everyone)
    if message.mentions_everyone != everyone:
        message.mentions_everyone = everyone
        message.save(update_fields=['mentions_everyone', 'updated_at'])

    return rows


def _allowed(message, participant_ids):
    """The ids that may really be stored, as Participant rows.

    ACTIVE members of THIS conversation only. The security half: an id is a
    number a client chose, and without the conversation filter a mention row
    would point from a message in one group at a membership in another - which
    is a way to ask "is this person in that group" and get an answer.

    The sender is dropped. Somebody naming themselves is not a mention; it
    would only ever produce a notification about your own message, which
    `notification/services.py:notify` refuses for the same reason.
    """
    wanted = {int(value) for value in participant_ids or [] if value is not None}
    wanted.discard(message.sender_participant_id)
    if not wanted:
        return []

    return list(
        Participant.objects.filter(
            pk__in=wanted,
            conversation_id=message.conversation_id,
            status=Participant.Status.ACTIVE,
        )
    )


def mentions_participant(message, participant):
    """Whether this message names this member.

    True for `@everyone` whoever they are, and otherwise only for a row that
    names them. Reads `message.mentions.all()`, so a caller looping over a
    group's members must prefetch it - `notification/push/outbound.py`
    does - or this is one query per member.

    The sender is never counted as mentioned, even under `@everyone`: they
    wrote the message, and the rest of the alert path would then have to
    remember to exclude them a second time.
    """
    if participant.pk == message.sender_participant_id:
        return False
    if message.mentions_everyone:
        return True
    return any(
        row.participant_id == participant.pk for row in message.mentions.all()
    )


def is_reply_to(message, participant):
    """Whether this message answers one of that member's messages.

    A reply and a mention are the same switch on the Notifications screen
    ("Mentions & replies"), so they are asked together and both live here.

    Matches on the membership rather than on the account, so a reply to a
    message somebody texted in from a number - no account, no `sender` - still
    counts as answering them.
    """
    parent = message.reply_to
    if parent is None:
        return False
    if parent.sender_participant_id == message.sender_participant_id:
        # Answering yourself. Nobody else is involved, so nobody is told.
        return False
    return parent.sender_participant_id == participant.pk
