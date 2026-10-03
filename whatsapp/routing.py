"""Which group an incoming WhatsApp message belongs to.

With SMS every group has its own number, so the number IS the group. Here one
business number carries every group its owner attached to it, and the same
person can be in several of them. So a message arriving on that number has to
be routed, and routing wrongly posts somebody's words into the wrong group -
which is the one mistake this module exists to prevent.

THE ORDER, strongest signal first:

    1. They named a group: `#family see you at 7`.
    2. They replied to a specific message (WhatsApp's swipe-to-reply). The
       quoted wamid is in our delivery log, which says which group it was.
    3. They are in exactly one group on this number.
    4. The group they last talked in, or last received a message from.
    5. None of the above: ask them. Nothing is posted.

Every candidate is re-checked against current membership, whichever rule found
it. A reply to a message from a group they have since left must not post into
that group.
"""
import re
from dataclasses import dataclass, field

from chat import membership
from chat.models import Participant
from whatsapp.models import GroupChannel, WhatsAppDelivery

_KEYWORD_PREFIX = re.compile(r'^\s*#([A-Za-z0-9]{1,16})\b\s*(.*)$', re.DOTALL)


@dataclass
class Route:
    channel: GroupChannel = None
    participant: Participant = None
    text: str = ''
    # Set when the sender named a group, so the caller can confirm a switch.
    explicit: bool = False
    # When no group could be chosen: the ones they could mean.
    options: list = field(default_factory=list)


def phone_of(wa_id):
    """Meta's wa_id is E.164 without the plus."""
    return f'+{wa_id}' if wa_id and not wa_id.startswith('+') else wa_id


def memberships(connection, wa_id):
    """[(channel, participant)] for every group on this number this person is in.

    A membership here is the one `chat.membership.resolve` picks - the single
    answer the rest of the product uses - and only if it is ACTIVE.
    """
    phone = phone_of(wa_id)
    found = []
    channels = (
        GroupChannel.objects.filter(
            whatsapp_connection=connection, kind=GroupChannel.Kind.WHATSAPP
        )
        .select_related('conversation')
        .order_by('pk')
    )
    for channel in channels:
        participant = membership.resolve(channel.conversation, phone_number=phone)
        if participant is not None and participant.status == Participant.Status.ACTIVE:
            found.append((channel, participant))
    return found


def route(connection, contact, text, *, context_wamid=''):
    """Decide where this message goes. Returns a Route."""
    candidates = memberships(connection, contact.wa_id)
    by_conversation = {channel.conversation_id: (channel, p) for channel, p in candidates}

    def pick(conversation_id, body, explicit=False):
        channel, participant = by_conversation[conversation_id]
        return Route(channel=channel, participant=participant, text=body, explicit=explicit)

    # A name beats a swipe: somebody who swipes to reply AND types `#work` has
    # said something more specific than the swipe.
    match = _KEYWORD_PREFIX.match(text or '')
    if match:
        keyword, rest = match.group(1).lower(), match.group(2)
        for channel, _participant in candidates:
            if channel.keyword == keyword:
                return pick(channel.conversation_id, rest, explicit=True)
        # An unknown #tag is just text - `#1 fan` is a sentence, not a command.

    if context_wamid:
        quoted = (
            WhatsAppDelivery.objects.filter(connection=connection, wamid=context_wamid)
            .values_list('conversation_id', flat=True)
            .first()
        )
        if quoted in by_conversation:
            return pick(quoted, text)

    if len(candidates) == 1:
        return pick(candidates[0][0].conversation_id, text)

    if contact.current_conversation_id in by_conversation:
        return pick(contact.current_conversation_id, text)

    return Route(text=text, options=[channel for channel, _ in candidates])
