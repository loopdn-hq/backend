"""The consent handshake with somebody who has no account.

    first contact   the message AND the question, in one email
    YES             they are in; both sides are told
    NO              they are out, for this conversation, for good
    anything else   one nudge, ever, then silence

WHY THE STATE LIVES ELSEWHERE. `chat/membership.py` owns every write to
`ChannelConsent` - `accept_email_consent` and `decline_email` - and this module
owns what happens AROUND those writes: the email back, the notification to the
sender, and the live update that unlocks their composer. Keeping the two apart
is what stops a second caller one day setting consent without telling anybody.

WHY EACH ANSWER GETS AN EMAIL. `mail/optout.py` already argues this for STOP
EMAILS and the argument is the same here: somebody who replies and hears
nothing does not know it worked, and the next thing they press is Report spam -
which is the one event that suppresses an address account-wide at the provider.
Silence is the expensive option, not the polite one.

THE NUDGE IS CAPPED IN THE DELIVERY LOG. Not in the cache, and not behind a new
column. `EmailDelivery` already records everything we send, so "have we nudged
this address?" is a question about what we actually sent - which is the honest
place for it, survives a cache flush, and costs no migration. One nudge per
address for the life of the conversation, so an auto-responder terminates:
it replies, we nudge once, it replies again, we say nothing.
"""
import logging

from django.conf import settings

from chat.membership import accept_email_consent, decline_email
from chat.models import ChannelConsent
from chat.services import conversation_group, send_to_group
from mail.models import EmailDelivery
from notification.models import Notification
from notification.services import notify
from utils.mail import (
    BRIDGE_ACCEPTED,
    BRIDGE_CONSENT_NUDGE,
    BRIDGE_DECLINED,
    EmailError,
    send_templated_email,
)

logger = logging.getLogger(__name__)

#: The reason written on the row that records a nudge. It is the cap: one row
#: with this reason means this address has had its one reminder.
NUDGE_REASON = 'consent nudge'


def accept(participant):
    """They replied YES. Record it, confirm it, and tell the sender."""
    if not accept_email_consent(participant):
        # Already opted in, or opted out and refused. Either way there is
        # nothing new to confirm and nobody to tell.
        return False

    _send(participant, BRIDGE_ACCEPTED, 'acceptance')
    _announce(participant, Notification.Type.EMAIL_OPTED_IN)
    return True


def decline(participant):
    """They replied NO. Record it, confirm it, and tell the sender."""
    changed = decline_email(participant)

    # Confirmed even when nothing changed. A second NO means they are still
    # asking, and answering the first but not the second is how somebody
    # decides we are ignoring them.
    _send(participant, BRIDGE_DECLINED, 'decline confirmation')

    if changed:
        _announce(participant, Notification.Type.EMAIL_DECLINED)
    return changed


def nudge(participant):
    """They replied with something that is not an answer. Remind them once.

    Returns True if a reminder went out. False means they have already had
    theirs, which is the cap doing its job.
    """
    if _already_nudged(participant):
        return False

    # Recorded BEFORE the send, not after. If the provider call raises, a
    # second unparseable reply must not produce a second reminder - the cap is
    # about how many times we speak, not how many times we succeed.
    _record_nudge(participant)
    _send(participant, BRIDGE_CONSENT_NUDGE, 'consent nudge')
    return True


def _already_nudged(participant):
    return EmailDelivery.objects.filter(
        participant=participant,
        direction=EmailDelivery.Direction.OUTBOUND,
        message__isnull=True,
        reason=NUDGE_REASON,
    ).exists()


def _record_nudge(participant):
    """A delivery row with no message. See the module docstring."""
    from mail.outbound import _domain, _hash_address, address_for

    address = address_for(participant) or ''
    return EmailDelivery.objects.create(
        direction=EmailDelivery.Direction.OUTBOUND,
        conversation=participant.conversation,
        message=None,
        participant=participant,
        user=participant.user,
        counterpart_domain=_domain(address),
        counterpart_hash=_hash_address(address),
        status=EmailDelivery.Status.SENT,
        reason=NUDGE_REASON,
        attempts=1,
    )


def _send(participant, template, what):
    """One email, and a failure that is logged rather than raised.

    Every caller is already past the point of no return - consent has been
    recorded - so a provider outage must not turn a decision that has been
    made into an exception thrown back at an inbound webhook.
    """
    from mail.outbound import (
        _conversation_label, _headers_for, address_for, from_header,
        reply_to_header,
    )
    from mail.tokens import unsubscribe_url
    from mail import commands

    address = address_for(participant)
    if not address:
        return

    conversation = participant.conversation
    stop_url = unsubscribe_url(participant)
    try:
        send_templated_email(
            template,
            to=address,
            context={
                'sender_name': _conversation_label(conversation),
                # Set for a group, so the three answers can talk about the
                # group rather than "your conversation with" a group's name.
                'group_name': (
                    None if conversation.is_external_direct else conversation.name
                ),
                'stop_keyword': commands.KEYWORD,
                'unsubscribe_url': stop_url,
                'app_url': settings.FRONTEND_URL,
            },
            from_email=from_header(conversation),
            # The membership's own reply address. Without it a reply went to
            # the From address, which takes no mail - so a YES sent in answer
            # to the reminder that asks for one was lost, and after YES,
            # "reply to these emails" pointed at nowhere.
            reply_to=reply_to_header(participant, conversation),
            headers=_headers_for(conversation, stop_url),
        )
    except EmailError:
        logger.warning(
            'Could not send %s to participant %s', what, participant.pk
        )


def _announce(participant, type):
    """Tell the sender, and nobody else.

    `notification/delivery_alerts.py` settled the audience question for this
    family already: the person who wrote the message is the one who will
    otherwise assume it arrived, and nobody else can act on the answer.

    The broadcast is what unlocks or locks their composer without a refresh.
    Sent through the conversation group their socket is already in, so there is
    no second subscription to manage.
    """
    conversation = participant.conversation
    owner = conversation.created_by
    if owner is None:
        return

    notify(owner, type, conversation=conversation)
    send_to_group(
        conversation_group(conversation.pk),
        {
            'type': 'conversation.consent',
            'conversation_id': conversation.pk,
            'participant_id': participant.pk,
            'status': _status(participant),
        },
    )


def _status(participant):
    row = (
        ChannelConsent.objects.filter(
            participant=participant, channel=ChannelConsent.Channel.EMAIL
        )
        .values_list('status', flat=True)
        .first()
    )
    return row or ChannelConsent.Status.PENDING
