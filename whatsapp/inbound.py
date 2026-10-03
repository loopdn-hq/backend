"""What happens when a WhatsApp message arrives on a connected business number.

One message in, one decision out, and at most one reply. No HTTP: the webhook
hands this an already-verified, already-deduplicated message.

ORDER
-----
  1. Record that they wrote. That opens WhatsApp's 24-hour window whatever
     else happens, because it is Meta's fact, not ours.
  2. STOP, START, HELP, GROUPS. Commands about the NUMBER - every group the
     owner runs on it - so they come before routing.
  3. Route to one group (whatsapp/routing.py). Ambiguous: ask, post nothing.
  4. Consent for THAT group. Only OPTED_IN posts.
  5. Post, as the member, with source=WHATSAPP. The ordinary fan-out then
     carries it to the app, SMS, email and the other WhatsApp members.

STOP IS PER BUSINESS NUMBER
---------------------------
On SMS, STOP to a group's number is STOP to that group, because the number is
the group. Here the number is the business, and the person reading "reply STOP
to opt out" has asked that business to stop - so STOP opts them out of every
group on this number, and START back into every one that was not DECLINED.
A NO to one group's invitation is a different answer and START does not
overwrite it.

REPLIES ARE CHEAP HERE, BUT NOT FREE OF MEANING
-----------------------------------------------
A reply inside the window is a free service message, so unlike SMS we can
afford to answer. We still answer a stranger with silence: a WhatsApp business
number is public, but confirming which groups a number is in is not.
"""
import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from chat import membership
from chat.models import ChannelConsent, Message, PostingNotAllowed
from chat.services import create_message
from whatsapp import routing
from whatsapp.models import WhatsAppContact

logger = logging.getLogger(__name__)

WHATSAPP = ChannelConsent.Channel.WHATSAPP

# Whole-message matches only, and only words nobody sends as conversation.
# "Yes", "Join" and "?" are deliberately NOT here: "Are you coming?" "Yes" is a
# message to the group, and swallowing it as a command would both lose it and
# change their consent - the failure sms/inbound.py documents for YES and NO.
# The invitation template's quick-reply button sends START, so nobody needs
# them anyway.
STOP_WORDS = {'stop', 'unsubscribe', 'opt out', 'optout'}
START_WORDS = {'start', 'subscribe', 'opt in', 'optin'}
HELP_WORDS = {'help'}
GROUPS_WORDS = {'groups'}


@dataclass
class InboundResult:
    action: str
    participant: object = None
    message: object = None
    reply: str = None


def touch_contact(connection, wa_id, profile_name=''):
    """The contact row for this sender, with their window freshly opened."""
    contact, _ = WhatsAppContact.objects.get_or_create(connection=connection, wa_id=wa_id)
    contact.last_inbound_at = timezone.now()
    fields = ['last_inbound_at', 'updated_at']
    if profile_name and profile_name != contact.profile_name:
        contact.profile_name = profile_name[:128]
        fields.append('profile_name')
    contact.save(update_fields=fields)
    return contact


@transaction.atomic
def handle_inbound(connection, wa_id, text, *, profile_name='', context_wamid=''):
    contact = touch_contact(connection, wa_id, profile_name)
    word = (text or '').strip().lower()

    if word in STOP_WORDS:
        return _stop(connection, contact)
    if word in START_WORDS:
        return _start(connection, contact)

    candidates = routing.memberships(connection, wa_id)
    if not candidates:
        # A stranger, or somebody no longer in any group here. Silence.
        return InboundResult(action='ignored_unknown_sender')

    if word in HELP_WORDS:
        return InboundResult(action='help', reply=_help_text(connection, candidates))
    if word in GROUPS_WORDS:
        return InboundResult(action='groups', reply=_groups_text(candidates))

    route = routing.route(connection, contact, text, context_wamid=context_wamid)
    if route.channel is None:
        return InboundResult(
            action='ambiguous',
            reply='Which group is that for? Start your message with its tag:\n'
                  + _tags([(c, None) for c in route.options]),
        )

    channel, participant = route.channel, route.participant
    conversation = channel.conversation

    if not membership.account_is_usable(participant):
        return InboundResult(action='ignored_banned_account', participant=participant)

    consent = membership.consent_status(participant, WHATSAPP)
    if consent in (ChannelConsent.Status.OPTED_OUT, ChannelConsent.Status.DECLINED):
        # They told us no. Nothing passes in either direction until they say
        # otherwise - and replying to say so would be a message they refused.
        return InboundResult(action='ignored_opted_out', participant=participant)
    if consent != ChannelConsent.Status.OPTED_IN:
        return InboundResult(
            action='ignored_pending_consent',
            participant=participant,
            reply=f'Reply START to get {conversation.name} messages here on WhatsApp.',
        )

    # Remembered BEFORE posting, so `#work` on its own switches groups too.
    if contact.current_conversation_id != conversation.pk:
        contact.current_conversation = conversation
        contact.save(update_fields=['current_conversation', 'updated_at'])

    body = (route.text or '').strip()
    if not body:
        if route.explicit:
            return InboundResult(
                action='switched', participant=participant,
                reply=f'Now talking in {conversation.name}.',
            )
        return InboundResult(action='ignored_empty', participant=participant)

    if not channel.replies_enabled:
        return InboundResult(
            action='refused_replies_off', participant=participant,
            reply=f'{conversation.name} does not take replies on WhatsApp.',
        )

    try:
        message = create_message(
            conversation=conversation,
            sender=participant.user,
            text=body,
            source=Message.Source.WHATSAPP,
            participant=participant,
        )
    except PostingNotAllowed:
        return InboundResult(
            action='refused_broadcast', participant=participant,
            reply=f'{conversation.name} is announcements only.',
        )
    return InboundResult(action='posted', participant=participant, message=message)


# --- commands --------------------------------------------------------------


def _stop(connection, contact):
    rows = routing.memberships(connection, contact.wa_id)
    for _channel, participant in rows:
        membership.set_channel_consent(participant, WHATSAPP, ChannelConsent.Status.OPTED_OUT)
    if not rows:
        return InboundResult(action='ignored_unknown_sender')
    return InboundResult(
        action='opted_out',
        reply='You will not get any more group messages from this number. Reply START to undo.',
    )


def _start(connection, contact):
    rows = routing.memberships(connection, contact.wa_id)
    if not rows:
        return InboundResult(action='ignored_unknown_sender')

    joined = []
    for channel, participant in rows:
        answer = membership.consent_status(participant, WHATSAPP)
        if answer == ChannelConsent.Status.DECLINED:
            continue
        if answer != ChannelConsent.Status.OPTED_IN:
            membership.set_channel_consent(participant, WHATSAPP, ChannelConsent.Status.OPTED_IN)
        joined.append((channel, participant))

    if not joined:
        return InboundResult(action='nothing_to_start')
    names = ', '.join(channel.conversation.name for channel, _ in joined)
    reply = f'You will get messages from {names} here. Reply STOP to opt out.'
    if len(joined) > 1:
        reply += '\nTo post, start with the group tag:\n' + _tags(joined)
    return InboundResult(action='opted_in', reply=reply)


def _tags(rows):
    return '\n'.join(f'#{channel.keyword} - {channel.conversation.name}' for channel, _ in rows)


def _groups_text(rows):
    return 'Your groups on this number:\n' + _tags(rows)


def _help_text(connection, rows):
    text = 'Reply to post in your group. STOP to opt out, START to opt back in.'
    if len(rows) > 1:
        text += ' Start a message with a tag to pick a group:\n' + _tags(rows)
    return text
