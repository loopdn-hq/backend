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

Archive is the one that has to be stated rather than assumed. It hides a
conversation from a list; it is not a request for silence. Muting is how
somebody asks for that, and a member who tidied a group away still expects the
text when somebody posts.
"""
from chat.models import ChannelConsent, Participant


def refusal_reason(participant, channel):
    """Why this member must not be sent this channel, or None if they may be.

    A string rather than a boolean because the reason is the useful half: the
    delivery log answers for its own omissions, and "muted" and "no consent"
    are very different conversations to have with somebody asking why their
    message did not arrive.

    Answers only the membership question. Whether there is a usable address is
    the channel's own business - see the module docstring.
    """
    if participant.status != Participant.Status.ACTIVE:
        return 'not an active member'

    if participant.muted:
        return 'muted'

    if not _channel_enabled(participant, channel):
        return f'{channel} disabled for this group'

    consent = consent_for(participant, channel)
    if consent is None:
        return 'no consent on record'
    if not consent.allows_delivery:
        return _consent_reason(consent)

    # Archived deliberately absent. See the module docstring.
    return None


def eligible_for(message, channel):
    """Split a message's audience into who may be sent it and who may not.

    Returns (send_to, skipped), where `skipped` pairs each member with the
    reason - the shape the SMS fan-out already logs, kept so its delivery
    records keep meaning the same thing.

    The sender is left out: they wrote it, and texting somebody their own
    message reads as an echo and bills for the privilege.
    """
    rows = (
        Participant.objects.filter(
            conversation=message.conversation,
            status=Participant.Status.ACTIVE,
        )
        .exclude(pk=message.sender_participant_id)
        .select_related('user__profile')
        .prefetch_related('channel_consents')
    )

    send_to, skipped = [], []
    for row in rows:
        reason = refusal_reason(row, channel)
        if reason is None:
            send_to.append(row)
        else:
            skipped.append((row, reason))
    return send_to, skipped


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
    # Email and voice have no per-membership switch yet. When they gain one it
    # belongs here, beside SMS's, rather than in the channel's own module.
    return True


def _consent_reason(consent):
    """The refusal, in the words the state actually means."""
    if consent.status == ChannelConsent.Status.OPTED_OUT:
        return 'opted out'
    if consent.status == ChannelConsent.Status.DECLINED:
        return 'declined this group'
    return 'consent not granted'
