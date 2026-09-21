"""Who should receive a message, on any channel.

ONE LADDER, NOT ONE PER CHANNEL
-------------------------------
Every external channel asks the same question first - is this person still a
member, have they muted the group, did they agree to this channel - and only
then a question of its own, which is whether it has an address it can use.

Written per channel, the shared half gets copied. Copies drift: the SMS code
learns that archiving must not silence a group and the email code learns it a
release later, and in between somebody who tidied a conversation away stops
getting email from it and nobody can say why. So the shared half lives here,
once, and each channel keeps only the part that is genuinely its own.

THE ORDER IS THE POLICY
-----------------------
Membership first, then the member's own settings, then consent. It reads the
way the rules were written down:

    left / removed / blocked   ->  deny, they are not in the group
    muted                      ->  deny, they asked not to be notified
    channel disabled           ->  deny, this channel is off for them
    no consent                 ->  deny, they have not agreed to it
    archived                   ->  DELIVER

One question is asked NEXT TO this ladder rather than inside it: whether the
recipient and the SENDER have blocked each other. That one is about a pair
rather than about a person, so it lives in `_block_reason` and is applied by
`eligible_for` - see the note there.

Archive is the one that has to be stated rather than assumed. It hides a
conversation from a list; it is not a request for silence. Muting is how
somebody asks for that, and a member who tidied a group away still expects the
text when somebody posts.

WHAT GOES OUT
-------------
Before anybody in a conversation is considered, the conversation itself is.
`conversation_reason` says whether the outside channels carry it at all, and
two kinds are carried:

    a GROUP             on every external channel
    an EXTERNAL 1:1     on email only - a thread whose far side is an address
                        rather than an account (`Conversation.is_external_direct`)

A one-to-one between two ACCOUNTS is still carried by none of them, and the
reason is the bug that produced this rule: every external channel is shaped for
a group - the email is signed "<group name> via Loopd" and ends with a footer
for leaving the group, the text goes out from the group's own number - so such a
conversation's email arrived from " via Loopd" and invited the reader to leave a
group that did not exist. The rule then said a direct message needed "its own
template and its own product decision". The external 1:1 now has both: its own
From line, its own List-Id and its own footer, in `mail/outbound.py`. Two
accounts still have neither, and need neither - both sides are already in the
app, and nobody outside it is waiting to be told.

SMS carries no one-to-one at all, external or not: every number the product
owns belongs to one Loop, with that Loop's consent and STOP handling around it.

CHANNELS REGISTER THEMSELVES
----------------------------
`chat` does not know which channels exist. Each one calls `register_channel`
from its own AppConfig.ready(), so adding voice is a new app and not an edit to
the path every message in the product goes through - see `schedule_all` at the
foot of this module.
"""
import logging

from chat.models import ChannelConsent, Conversation, Participant

logger = logging.getLogger(__name__)


def membership_reason(participant):
    """Why this member must not be told about anything here, or None.

    The rungs that are about the MEMBER rather than about a channel: are they
    still in the group, and did they ask for silence. Split out of
    `refusal_reason` because push needs exactly these two and none of the rest
    - it has no consent row to read, since the browser permission prompt is the
    consent - and a second copy of "active and not muted" is the kind of thing
    that gets fixed in one place and not the other.

    Mute is here, not in the channel half, deliberately: muting a Loop means no
    interruptions from it by any means, and a mention that broke through would
    make the switch a lie.
    """
    if participant.status != Participant.Status.ACTIVE:
        return NOT_A_MEMBER

    if participant.muted:
        return MUTED

    return None


def refusal_reason(participant, channel):
    """Why this member must not be sent this channel, or None if they may be.

    A string rather than a boolean because the reason is the useful half: the
    delivery log answers for its own omissions, and "muted" and "no consent"
    are very different conversations to have with somebody asking why their
    message did not arrive.

    Answers only the membership question. Whether there is a usable address is
    the channel's own business - see the module docstring.
    """
    reason = membership_reason(participant)
    if reason is not None:
        return reason

    if not _channel_enabled(participant, channel):
        return f'{channel} disabled for this group'

    consent = consent_for(participant, channel)
    if consent is None:
        return NO_CONSENT
    if not consent.allows_delivery:
        if _is_the_consent_request(participant, consent):
            # The one send that a PENDING answer does not refuse, because this
            # send IS the question. An external one-to-one asks by email - the
            # message and the request travel together - so refusing here would
            # mean the question could never be put and the answer could never
            # be given.
            #
            # Narrow on purpose: only a row with no account, only while nothing
            # has been asked yet (`consent_sent_at`), and only in a thread
            # opened against an address. A group invitee is still refused,
            # because nothing in the product asks them for consent yet.
            return None
        return _consent_reason(consent)

    # Archived deliberately absent. See the module docstring.
    return None


def _is_the_consent_request(participant, consent):
    """Whether this send is the first contact that asks for consent.

    The conversation is only consulted once the cheap tests have passed - an
    account row is seeded OPTED_IN and never reaches here, so a group fan-out
    does not pay for this.
    """
    if consent.status != ChannelConsent.Status.PENDING:
        return False
    if participant.user_id is not None or not participant.email:
        return False
    if participant.consent_sent_at is not None:
        return False
    return participant.conversation.is_external_direct


def retry_refusal(participant, message, channel, address_reason):
    """Why a FAILED delivery must not be tried again now, or None if it may be.

    Everything a fresh fan-out asks, asked again: whether the conversation is
    still carried at all, the ladder, the block screen, and the channel's own
    address question - which is passed in, because it is the one part that is
    the channel's. `sms/outbound.py:still_eligible` and its email twin are each
    one line calling this, so the two channels cannot come to re-decide a
    retry differently.

    It has to be asked again. A retry fires minutes or hours after the message
    was written, and the reason it failed - a provider having a bad minute -
    says nothing about whether the person still wants it. Inside that window
    they can say STOP, mute the group, leave it, be removed from it, or block
    the sender. A decision taken at fan-out time and trusted later is how
    somebody who said no is reached anyway.

    `participant` may be None (the row was deleted) and `message` may be None
    (a notice, or a deleted message); both are answered rather than raised on,
    because the caller is a sweep over hundreds of rows.
    """
    if participant is None:
        return 'the membership is gone'

    if message is not None:
        reason = conversation_reason(message.conversation, channel)
        if reason is not None:
            return reason

    return (
        refusal_reason(participant, channel)
        or sender_block_reason(participant, message)
        or address_reason(participant)
    )


def conversation_reason(conversation, channel=None):
    """Why this conversation is not carried outside the app, or None.

    None for a group, and None for an EXTERNAL one-to-one on a channel that
    carries one. Otherwise a reason in the ladder's own idiom, asked before any
    member is considered because it is still a fact about the conversation and
    not about anybody in it - see WHAT GOES OUT in the module docstring.

    `channel` is optional so a caller with no particular channel in mind still
    gets the conversation-level answer. Given one, the answer narrows: email
    carries an external thread and SMS does not, because every number the
    product owns belongs to a Loop.

    Asked in two places, on purpose. `schedule_all` asks so that no work is
    queued: a busy day of direct messages would otherwise hand a worker one
    empty fan-out per channel per message. `eligible_for` asks so that a
    fan-out reached any other way - a task re-run from a shell, a "resend"
    button written later - still finds nobody. One rule, defined here, applied
    at the door and again at the desk.
    """
    if conversation.kind == Conversation.Kind.GROUP:
        return None

    if conversation.is_external_direct:
        if channel is None or carries_external(channel):
            return None
        return f'{channel} does not carry external conversations'

    return f'{conversation.kind} conversations are not delivered externally'


def audience(message, *, select_related=(), prefetch_related=()):
    """Everybody who could be told about this message, and the block screen.

    Returns `(rows, screen)`: the ACTIVE members of the conversation except the
    sender, and the one screen the whole fan-out is judged against. Each caller
    passes the joins ITS decision needs - consent rows for SMS and email, the
    notification settings and browser subscriptions for push - so the shared
    part is the part that is genuinely shared.

    Two queries plus the screen, whatever the size of the group. The sender is
    left out here rather than by each caller: texting somebody their own
    message reads as an echo and bills for the privilege, and a caller that
    forgot would be a caller that did it.
    """
    rows = (
        Participant.objects.filter(
            conversation=message.conversation,
            status=Participant.Status.ACTIVE,
        )
        .exclude(pk=message.sender_participant_id)
        .select_related(*select_related)
        .prefetch_related(*prefetch_related)
    )

    # One query for the whole audience, before the loop. See screen_for_message.
    return rows, screen_for_message(message)


def eligible_for(message, channel):
    """Split a message's audience into who may be sent it and who may not.

    Returns (send_to, skipped), where `skipped` pairs each member with the
    reason - the shape the SMS fan-out already logs, kept so its delivery
    records keep meaning the same thing.

    The sender is left out: they wrote it, and texting somebody their own
    message reads as an echo and bills for the privilege.
    """
    if conversation_reason(message.conversation, channel) is not None:
        # Nobody - and nothing written to the delivery log either. The log
        # answers for omissions among a group's members, and this is not an
        # omission: nothing in the product promises a direct message will be
        # texted or emailed. A row per direct message per channel would bury
        # the one place that has to stay legible.
        return [], []

    rows, screen = audience(
        message,
        select_related=('user__profile', 'user__messaging_settings'),
        prefetch_related=('channel_consents',),
    )

    send_to, skipped = [], []
    for row in rows:
        reason = refusal_reason(row, channel) or block_reason(row, screen)
        if reason is None:
            send_to.append(row)
        else:
            skipped.append((row, reason))
    return send_to, skipped


#: What the delivery log records when a block is why nothing was sent. Precise
#: rather than vague because these tables are staff-only - they appear in no
#: serializer, no viewset and no URL, only the admin - and "why did I not get
#: that" is the question the log exists to answer.
BLOCKED_REASON = 'blocked between sender and recipient'

#: The ladder's own words, as constants so the two channels' maintenance
#: sweeps can name them without copying the strings - see POLICY_REASONS.
NOT_A_MEMBER = 'not an active member'
MUTED = 'muted'
NO_CONSENT = 'no consent on record'
OPTED_OUT = 'opted out'
DECLINED = 'declined this group'
CONSENT_NOT_GRANTED = 'consent not granted'

#: Every reason that is a DECISION rather than a failure. A row carrying one of
#: these is never worth retrying: the answer will be the same next hour, and
#: re-deciding it is how somebody who said no gets reached anyway. Each channel
#: adds its own address and provider reasons to this in its maintenance module.
POLICY_REASONS = (
    NOT_A_MEMBER, MUTED, NO_CONSENT, OPTED_OUT, DECLINED, CONSENT_NOT_GRANTED,
    BLOCKED_REASON,
)


def screen_for_message(message):
    """The accounts that must not be told about THIS message's sender.

    The only place that knows how a sender is shaped, which is two ways:

        an account      `message.sender`
        a bare number   `message.sender_participant.phone_number`, for somebody
                        with no account who texted in - `sender` is NULL for
                        them, and they are most of the members of most groups
        a bare address  `message.sender_participant.email`, the same person
                        arriving by the other channel

    Built once per message and read per recipient, so a fan-out stays two
    queries plus one however large the group is.
    """
    from user.blocks import screen_for_sender

    participant = message.sender_participant
    account_less = participant is not None and participant.user_id is None
    return screen_for_sender(
        user_id=message.sender_id,
        phone_number=participant.phone_number if account_less else None,
        email=participant.email if account_less else None,
    )


def sender_block_reason(participant, message):
    """Whether this ONE recipient is screened from this ONE message's sender.

    The single-recipient door, for a caller holding one of each and no audience
    to amortise a screen over - `sms/outbound.py:still_eligible`, which re-asks
    the whole decision when a failed text is retried hours later.

    A fan-out must NOT use this: it would build a screen per recipient and turn
    two queries into two hundred. `eligible_for` builds one screen and reads it
    per row instead.
    """
    if participant is None or message is None:
        return None
    return block_reason(participant, screen_for_message(message))


def block_reason(participant, screen):
    """Whether this recipient is screened from that sender.

    WHY THIS IS NOT A RUNG IN `refusal_reason`. Every rung up there is a fact
    about the row in front of it - their status, their mute, their consent. A
    block is a fact about a PAIR, and the function's own docstring promises it
    "answers only the membership question". Smuggling a second person in
    through a keyword argument would make that promise false and would let a
    caller forget to pass one, which is the same thing as not checking.

    So it lives here, one line below the `.exclude(sender)` in `eligible_for`
    that already subtracts somebody from an audience for a relational reason.

    ON ORDER: S19 of the blocking spec puts this at step 4, above channel and
    consent, and it is asked after them instead. The outcome is identical -
    both refuse - and only the reason written to the delivery log differs. That
    is the better way round anyway: a member who muted the group AND blocked
    the sender is logged as muted, which is the fact they chose, and the log
    stays quieter about who has blocked whom.

    A phone-only recipient is never screened: `UserBlock.blocker` is a
    non-nullable FK to User, so somebody with no account cannot have blocked
    anybody. That reasoning is about the BLOCKER's side, and it still holds -
    which is why an address-only recipient IS screened here. Nobody is claiming
    they blocked anyone; the question is whether this sender blocked THEM, and
    `screen.emails` holds exactly the addresses for which the answer is yes.
    """
    if not screen:
        return None
    if participant.user_id is not None:
        return BLOCKED_REASON if participant.user_id in screen.users else None
    if participant.email:
        return BLOCKED_REASON if participant.email in screen.emails else None
    return None


def consent_for(participant, channel):
    """This member's answer for one channel, or None if never asked.

    Reads the prefetch when there is one, so a fan-out over two hundred members
    is one query rather than two hundred.
    """
    for consent in participant.channel_consents.all():
        if consent.channel == channel:
            return consent
    return None


def _channel_enabled(participant, channel):
    """Whether this channel is switched on for this membership at all.

    Distinct from consent: this is a delivery setting, which an admin screen
    also touches, and it lives where each channel already keeps it.
    """
    if channel == ChannelConsent.Channel.SMS:
        return participant.sms_enabled

    # EMAIL DELIBERATELY HAS NO SWITCH HERE, and should not gain one.
    #
    # SMS needs both a switch and an answer for reasons email does not share:
    # `sms_enabled` is also written by an admin screen, and a carrier STOP
    # sweeps it across every group a phone-only row belongs to. Email has
    # neither - `mail/outbound.py` refuses every row with no account, so there
    # are no account-less email recipients, and there is no carrier sweep.
    #
    # A `Participant.email_enabled` column would therefore have one writer and
    # one reader and would always agree with the consent row. Two fields that
    # must agree is the exact shape of the bug `sms/outbound.py:send_notice`
    # was fixed for: it read consent alone, the two disagreed, and the notice
    # believed the wrong one. The member's own answer belongs in one place.
    #
    # An ADMIN turning a group's email off is a different question and belongs
    # on the Conversation, beside `sms_delivery_enabled`, not here.
    return True


def _consent_reason(consent):
    """The refusal, in the words the state actually means."""
    if consent.status == ChannelConsent.Status.OPTED_OUT:
        return OPTED_OUT
    if consent.status == ChannelConsent.Status.DECLINED:
        return DECLINED
    return CONSENT_NOT_GRANTED


# --- the registry ----------------------------------------------------------
#
# One entry per installed channel. Held in a module global rather than read
# from settings because the list is a fact about which apps are installed, and
# an app that is installed but forgot to register is a bug worth having no
# second place to hide.

_channels = {}

#: The channels that carry a DIRECT conversation too. Kept beside `_channels`
#: rather than inside it so that a test patching `_channels` with a plain
#: {name: scheduler} dict still works; a name that is not in here is
#: group-shaped, which is the safe default.
_carries_direct = set()
# Channels that carry a ONE-TO-ONE conversation whose far side is an address
# rather than an account. Narrower than `_carries_direct`: push carries every
# direct message because it goes to a device about a thread the person is
# already in, while email carries only the external kind, because that is the
# one with somebody outside the app to deliver to.
_carries_external = set()


def register_channel(name, scheduler, *, groups_only=True, external=False):
    """Announce that this channel wants a copy of every new message.

    `scheduler` is called with the Message and is responsible for queueing its
    own fan-out after the transaction commits - never for sending anything
    inline. Registering the same name twice replaces it, which is what keeps a
    reloaded AppConfig from doubling the fan-out.

    `groups_only` is what ONLY A GROUP GOES OUT (see the module docstring)
    actually means: SMS and email are shaped for a group - the name in the
    From line, the group's number, the footer for leaving it - and have
    nothing to say about a direct message. Push is not shaped that way. It goes
    to the person's own device, about a conversation they are already in, so it
    carries a direct message as naturally as a group one and registers with
    `groups_only=False`.

    `external=True` is the narrower claim email makes: not "I carry direct
    messages" but "I carry the one-to-one threads whose far side is an address",
    which are the only ones with somebody outside the app to deliver to.
    """
    _channels[name] = scheduler
    if groups_only:
        _carries_direct.discard(name)
    else:
        _carries_direct.add(name)
    if external:
        _carries_external.add(name)
    else:
        _carries_external.discard(name)


def carries_external(name):
    """Whether this channel carries an external one-to-one conversation.

    False for a name that was never registered, which keeps the promise
    `registered_channels` relies on: a test may patch `_channels` with a plain
    dict and nothing here will raise on a name it has not seen.
    """
    return name in _carries_external


def registered_channels():
    """The channel names currently installed, for checks and diagnostics."""
    return sorted(_channels)


def schedule_all(message):
    """Offer one new message to every installed channel.

    Nothing raised by a channel may escape. This is called from the message
    write path, so one channel's misconfiguration must not fail a message that
    is already stored and already visible in the app to everybody else - and it
    must not stop the other channels either, which is why the loop catches per
    channel rather than around the whole thing.
    """
    for name, scheduler in list(_channels.items()):
        reason = conversation_reason(message.conversation, name)
        if reason is not None and name not in _carries_direct:
            logger.debug(
                'Message %s not offered to %s: %s', message.pk, name, reason
            )
            continue

        try:
            scheduler(message)
        except Exception:
            logger.exception(
                'Could not schedule %s delivery for message %s', name, message.pk
            )
