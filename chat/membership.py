"""Getting into a group, and out of it again.

One entry point per action, so that the web endpoints and the inbound-SMS
commands cannot drift apart. `JOIN`, `LEAVE`, `MUTE` and `UNMUTE` arriving as a
text land on exactly the functions the REST API calls; the only difference is
who the caller is and how they were identified.

That matters more than it looks. The rules about who may join are the rules that
keep a private group private, and a second copy of them written for SMS would be
a second place to get them wrong - in the path that is hardest to test and
reaches real phone numbers.

Nothing here is a DRF exception, for the same reason as the provider clients:
this has to stay usable from a management command, a Celery task and the webhook
handler, none of which have a request. loopd/exceptions.py maps them.
"""
import logging
from urllib.parse import quote

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from datetime import timedelta

from django.utils import timezone

from chat.models import (
    BlockedFromConversation,
    ChannelConsent,
    Conversation,
    Participant,
    local_part,
    mask_number,
)
from notification.models import Notification
from notification.services import notify

logger = logging.getLogger(__name__)


class JoinRefused(Exception):
    """The group will not take this person by this route.

    The message is written for the person who asked - it is read out over SMS -
    so it must never say anything a non-member should not learn. "That code is
    not right" is fine; "that group is invite-only and has 14 members" is not.
    """


class NotAMember(Exception):
    """The action needs an existing membership and there is not one."""


@transaction.atomic
def request_to_join(conversation, *, user=None, phone_number=None,
                    display_name='', code=None):
    """Somebody outside the group asks to come in.

    Exactly one of `user` (a web account) or `phone_number` (a text) identifies
    them. Returns the Participant, whose `status` says what actually happened:
    ACTIVE for a group they may walk into, PENDING for one where an admin
    decides.

    Idempotent: asking twice does not create a second row or a second request.
    """
    if conversation.kind != Conversation.Kind.GROUP:
        raise JoinRefused('You cannot join a one-to-one conversation.')

    if (user is None) == (phone_number is None):
        raise ValueError('Identify the joiner by exactly one of user or phone_number.')

    existing = _find_row(conversation, user=user, phone_number=phone_number)

    # Checked before the mode, and before the code: a blocked person must not
    # be able to tell a wrong code from a right one, or they learn whether
    # guessing is worth continuing.
    if existing is not None and existing.status == Participant.Status.BLOCKED:
        raise BlockedFromConversation('You cannot join this conversation.')

    if existing is not None and existing.status in (
        Participant.Status.ACTIVE,
        Participant.Status.PENDING,
    ):
        return existing

    mode = conversation.join_mode
    # One message for both refusals, word for word. A distinguishable "that
    # group is invite-only" confirms the code is real, which turns the join
    # endpoint into an oracle for harvesting valid codes.
    if mode == Conversation.JoinMode.INVITE or (
        mode == Conversation.JoinMode.CODE and not _code_matches(conversation, code)
    ):
        raise JoinRefused('That group code is not right.')

    status = (
        Participant.Status.PENDING
        if mode == Conversation.JoinMode.APPROVAL
        else Participant.Status.ACTIVE
    )

    def _announce_if_pending(row):
        """Tell the admins, once the row is really committed."""
        if row.status != Participant.Status.PENDING:
            return
        from chat.services import broadcast_join_request
        from notification.services import notify_many

        # The socket frame tells admins who are looking right now; the
        # notification is what an admin who was not finds waiting for them.
        notify_many(
            admin_users(conversation),
            Notification.Type.JOIN_REQUESTED,
            from_user=row.user,
            conversation=conversation,
            # The membership row, so an admin can approve or decline straight
            # from the notification. Every review endpoint is keyed on
            # Participant.pk rather than the user's id - the two differ as soon
            # as somebody belongs to more than one group, and a phone-only
            # requester has no user id at all.
            data={
                'participant_id': row.pk,
                **(
                    {'display_name': row.display_name}
                    if row.user_id is None
                    else {}
                ),
            },
        )
        transaction.on_commit(lambda: broadcast_join_request(conversation, row))

    if existing is not None:
        # They left or were removed once. Reuse the row - UNIQUE(conversation,
        # user) forbids a second, and their old messages still point at it.
        fields = ['status']
        if user is not None and existing.user_id is None:
            # The row we found is their old phone-only membership. Attach the
            # account, or the caller is told "joined" while the row still
            # belongs to nobody - so `has_participant` stays False and they
            # cannot see the group they were just let into.
            existing.user = user
            fields.append('user')
        existing.status = status
        existing.save(update_fields=fields)
        _announce_membership(existing)
        _announce_if_pending(existing)
        return existing

    if user is not None:
        created = Participant.objects.create(
            conversation=conversation,
            user=user,
            status=status,
            role=Participant.Role.MEMBER,
        )
        _announce_membership(created)
        _announce_if_pending(created)
        return created

    # A claimed row can still carry a number its owner has since replaced, and
    # carriers reassign surrendered numbers. The new owner is a different human,
    # so the stale number is released rather than colliding with it.
    Participant.objects.filter(
        conversation=conversation, phone_number=phone_number, user__isnull=False
    ).update(phone_number=None)

    created_by_text = Participant.objects.create(
        conversation=conversation,
        phone_number=phone_number,
        # Masked, not raw. This label is read out in the body of every message
        # the group sends, so storing the full number here would publish it to
        # every other member. An admin can rename them later.
        display_name=display_name or mask_number(phone_number),
        status=status,
        role=Participant.Role.MEMBER,
        # They texted us first, so they have asked for this by definition -
        # which is the whole reason JOIN is a better front door than adding
        # somebody's number on their behalf and hoping they consent later.
        sms_enabled=True,
    )
    # Texting JOIN is both the question and the answer, so both are recorded:
    # what they sent, and that it counted as agreement. set_channel_consent
    # stamps `consented_at` itself.
    record_consent_asked(
        created_by_text,
        ChannelConsent.Channel.SMS,
        'Sent JOIN to the group number.',
    )
    set_channel_consent(
        created_by_text,
        ChannelConsent.Channel.SMS,
        ChannelConsent.Status.OPTED_IN,
    )
    _announce_if_pending(created_by_text)
    return created_by_text


@transaction.atomic
def approve(participant, by):
    """An admin lets a pending request in.

    Re-read under a row lock before writing. Two admins looking at the same
    pending list, one blocking and one approving, would otherwise both succeed
    on their stale copy and the last write would put a blocked person in.
    """
    _require_admin(participant.conversation, by)
    participant = _locked(participant)
    if participant.status != Participant.Status.PENDING:
        raise JoinRefused('That request is no longer pending.')

    # Re-resolve across both identifiers. A pending request can be the same
    # person as a row blocked under their other one - they texted JOIN from a
    # phone, were blocked, then asked again from a freshly registered account -
    # and the admin reviewing the queue has no way to see that.
    decisive = resolve(
        participant.conversation,
        user=participant.user,
        phone_number=participant.phone_number,
    )
    if decisive is not None and decisive.status == Participant.Status.BLOCKED:
        raise BlockedFromConversation('That person is blocked from this group.')

    participant.status = Participant.Status.ACTIVE
    participant.save(update_fields=['status'])
    _announce_membership(participant)
    notify(
        participant.user,
        Notification.Type.JOIN_APPROVED,
        from_user=by,
        conversation=participant.conversation,
    )
    _tell_them(
        participant,
        f'You are in {participant.conversation.name}. '
        'Text to post. HELP for commands, STOP to stop texts from this group.',
    )
    return participant


@transaction.atomic
def decline(participant, by):
    """An admin turns a pending request down.

    Recorded as REMOVED rather than deleted, so that asking again is a new
    decision an admin makes rather than something that quietly re-queues.
    """
    _require_admin(participant.conversation, by)
    participant = _locked(participant)
    if participant.status != Participant.Status.PENDING:
        raise JoinRefused('That request is no longer pending.')
    participant.status = Participant.Status.REMOVED
    participant.save(update_fields=['status'])
    _revoke_socket(participant)
    notify(
        participant.user,
        Notification.Type.JOIN_DECLINED,
        from_user=by,
        conversation=participant.conversation,
    )
    _tell_them(
        participant,
        f'Your request to join {participant.conversation.name} was not accepted.',
    )
    return participant


@transaction.atomic
def leave(participant):
    """A member takes themselves out of a GROUP. Their messages stay.

    Refused on a one-to-one conversation. There is no way back into one: it is
    reached through `start_direct`, which finds the existing row by `direct_key`
    and never reactivates a membership, so leaving would lock somebody out of a
    thread permanently while the other person kept writing to it.

    Refused for the last admin, for the same reason an admin cannot be removed:
    a group with no admin has no route back to having one, and every pending
    request in it becomes permanently unapprovable.
    """
    conversation = participant.conversation
    if conversation.kind != Conversation.Kind.GROUP:
        raise JoinRefused('You cannot leave a one-to-one conversation.')

    if participant.status != Participant.Status.ACTIVE:
        raise NotAMember('You are not a member of that conversation.')

    if (
        participant.role == Participant.Role.ADMIN
        and _admin_count(conversation, lock=True) <= 1
    ):
        raise JoinRefused(
            'Make somebody else an admin before you leave this group.'
        )

    participant.status = Participant.Status.LEFT
    participant.save(update_fields=['status'])
    _revoke_socket(participant)
    return participant


@transaction.atomic
def remove(participant, by, *, block=False):
    """An admin takes somebody out, optionally for good.

    An admin cannot remove themselves this way - that is `leave` - and the last
    admin cannot be removed at all, or the group is left with nobody who can
    approve a request, add a member or change a setting.
    """
    conversation = participant.conversation
    _require_admin(conversation, by)

    if participant.user_id is not None and participant.user_id == by.pk:
        raise JoinRefused('Use leave to take yourself out of a group.')

    if (
        participant.role == Participant.Role.ADMIN
        and _admin_count(conversation, lock=True) <= 1
    ):
        raise JoinRefused('A group needs at least one admin.')

    # Never downgrade a block. `remove` without `block` on somebody already
    # blocked would otherwise quietly readmit them to every route that only
    # refuses BLOCKED.
    if participant.status == Participant.Status.BLOCKED and not block:
        return participant

    participant.status = (
        Participant.Status.BLOCKED if block else Participant.Status.REMOVED
    )
    participant.save(update_fields=['status'])
    _revoke_socket(participant)
    notify(
        participant.user,
        Notification.Type.MEMBER_REMOVED,
        from_user=by,
        conversation=participant.conversation,
        data={'blocked': True} if block else None,
    )
    return participant


@transaction.atomic
def unblock(participant, by):
    """An admin lifts a block, so the person may come back.

    Lands on REMOVED, not ACTIVE. Unblocking says they MAY return, not that
    they have: they come back the way anybody outside the group does - an admin
    adds them again, or they use the code or text JOIN as the group's join mode
    allows. Putting them straight back in would add somebody to a group without
    asking them, which nothing else here does.

    Re-read under a row lock, like `approve`, so an unblock and a fresh block
    racing on the same row cannot both act on a stale copy.
    """
    _require_admin(participant.conversation, by)
    participant = _locked(participant)
    if participant.status != Participant.Status.BLOCKED:
        raise JoinRefused('That person is not blocked from this group.')

    participant.status = Participant.Status.REMOVED
    participant.save(update_fields=['status'])
    return participant


@transaction.atomic
def set_role(participant, role, by):
    """Promote somebody to admin, or demote them back to a member.

    This is the way out of a group for its only admin. `leave` and `remove`
    both refuse to take the last one - a group with no admin has no route back
    to having one, because promoting somebody is itself an admin action - so
    without a handover the founder of a group is stuck in it permanently.

    A phone-only member cannot be an admin: every admin action is an
    authenticated REST call, so the role would be unusable, and it would count
    towards the last-admin guard while being unable to satisfy it.
    """
    conversation = participant.conversation
    _require_admin(conversation, by)

    if conversation.kind != Conversation.Kind.GROUP:
        raise JoinRefused('A one-to-one conversation has no roles.')

    if role not in Participant.Role.values:
        raise JoinRefused('That is not a role.')

    participant = _locked(participant)

    if participant.status != Participant.Status.ACTIVE:
        raise NotAMember('That person is not a member of this group.')

    if role == Participant.Role.ADMIN and participant.user_id is None:
        raise JoinRefused('Somebody with no account cannot be an admin.')

    demoting_last_admin = (
        participant.role == Participant.Role.ADMIN
        and role != Participant.Role.ADMIN
        and _admin_count(conversation, lock=True) <= 1
    )
    if demoting_last_admin:
        raise JoinRefused('A group needs at least one admin.')

    participant.role = role
    participant.save(update_fields=['role'])
    notify(
        participant.user,
        Notification.Type.ROLE_GRANTED
        if role == Participant.Role.ADMIN
        else Notification.Type.ROLE_REVOKED,
        from_user=by,
        conversation=conversation,
    )
    return participant


def set_delivery(participant, *, muted=None, sms_enabled=None, email_enabled=None):
    """Change how one member receives one conversation.

    Only what was asked for is written, so a client toggling the mute switch
    cannot accidentally reset somebody's SMS preference back to the default.

    The three switches do not live in the same place, and that is not an
    inconsistency. `muted` and `sms_enabled` are columns on the membership;
    email's answer is a ChannelConsent row, because consent is per channel and
    a column per channel would mean a migration every time one is added. This
    function is where that stops mattering to a caller.
    """
    if participant.status != Participant.Status.ACTIVE:
        raise NotAMember('You are not a member of that conversation.')

    fields = []
    if muted is not None:
        participant.muted = muted
        fields.append('muted')
    if sms_enabled is not None:
        participant.sms_enabled = sms_enabled
        fields.append('sms_enabled')

    if fields:
        participant.save(update_fields=fields)

    if email_enabled is not None:
        set_group_email(participant, email_enabled)
    return participant


def clear_history(participant):
    """Empty one member's view of a conversation, without them leaving it.

    They stay a member and everyone else's copy is untouched: this moves their
    own start-of-history to the newest message, so the thread reads as empty
    for them and the conversation drops out of their list.

    It comes back on its own when somebody posts, showing only what arrived
    after - which is why this is a pointer and not a delete. Nothing is
    destroyed, and the member has not been quietly removed from a group whose
    other members still think they are in it.

    The read pointer moves with it. Otherwise the cleared messages would still
    be counted as unread, and the conversation would come back carrying a badge
    for messages the member can no longer see.

    An EMPTY conversation is cleared too. There is no message to point at, so
    only the timestamp is written; the row leaves their list all the same, and
    returns when somebody finally posts.
    """
    if participant.status != Participant.Status.ACTIVE:
        raise NotAMember('You are not a member of that conversation.')

    # Recorded on every clear, and the ONLY thing recorded when there are no
    # messages to point at - see Participant.cleared_at. Without it, clearing
    # an empty conversation wrote nothing and the row returned on refresh.
    fields = ['cleared_at']
    participant.cleared_at = timezone.now()

    newest = participant.conversation.messages.order_by('-pk').first()
    if newest is None:
        participant.save(update_fields=fields)
        return participant

    if participant.cleared_before_message_id != newest.pk:
        participant.cleared_before_message = newest
        fields.append('cleared_before_message')
    if (participant.last_read_message_id or 0) < newest.pk:
        participant.last_read_message = newest
        fields.append('last_read_message')

    participant.save(update_fields=fields)
    return participant


def set_channel_consent(participant, channel, status, *, when=None):
    """Record this member's answer about one channel. The only way to set it.

    One entry point, for the reason the rest of this module has one: the REST
    API, an inbound text and a Celery sweep all have to leave the same record,
    and three copies of "and also stamp consented_at" is three chances to
    forget one. A consent record that cannot say when somebody agreed is not
    much of a record.
    """
    consent, _ = ChannelConsent.objects.get_or_create(
        participant=participant, channel=channel
    )

    stamp = when or timezone.now()
    consent.status = status
    if status == ChannelConsent.Status.OPTED_IN:
        consent.consented_at = stamp
    elif status in (
        ChannelConsent.Status.OPTED_OUT,
        ChannelConsent.Status.DECLINED,
    ):
        # DECLINED belongs here too. It used to stamp nothing at all, which
        # contradicts the paragraph above: when somebody said NO is exactly
        # what a consent complaint turns on, and it is the harder of the two
        # to reconstruct afterwards - an opt-in usually has an invitation
        # beside it, a refusal has nothing.
        consent.opted_out_at = stamp

    consent.save(update_fields=['status', 'consented_at', 'opted_out_at', 'updated_at'])
    return consent


def accept_email_consent(participant):
    """They replied YES. The one-way IN, and the mirror of `decline_email`.

    Reached from PENDING - the ordinary case - and from DECLINED, because
    changing your mind back is allowed and `invite_by_phone` already documents
    the same pair on the other channel: NO turns a group off, YES turns it on
    again.

    NEVER from OPTED_OUT, and that is the whole guard. A provider complaint or
    a STOP is about this address everywhere, not about one conversation, and a
    YES to one sender must not quietly undo it. `sms/inbound.py` learned this
    the expensive way - see the paragraph there about a word the carrier does
    not recognise for that purpose.

    Returns True when something actually changed, so a caller can stay quiet
    about a repeat rather than sending a second acknowledgement.
    """
    already = consent_status(participant, ChannelConsent.Channel.EMAIL)
    if already == ChannelConsent.Status.OPTED_OUT:
        logger.info(
            'Not accepting email consent for participant %s: opted out',
            participant.pk,
        )
        return False

    set_channel_consent(
        participant, ChannelConsent.Channel.EMAIL, ChannelConsent.Status.OPTED_IN
    )
    # No Participant.consent_at write. set_channel_consent above already
    # stamped `consented_at` on the email row, which is the per-channel record
    # this used to duplicate onto a single member-wide field.
    return already != ChannelConsent.Status.OPTED_IN


def record_consent_asked(participant, channel, wording):
    """Stamp when this member was asked about THIS channel, and in what words.

    Called once the consent request has actually been accepted by the provider,
    not when it was composed: `asked_at` gates whether a bounced first attempt
    may be asked again, and a send that never happened must not count as an ask.

    `consent_text` keeps the wording verbatim because "what exactly were they
    agreeing to" is the question a complaint turns on, and the template will
    have changed by the time anybody asks.

    PER CHANNEL, on the row that holds the answer. The same person can be
    invited to a group by text and later emailed about it, and those are two
    different questions asked on two different days. One pair of fields on
    Participant could hold only whichever came last, so the earlier record was
    silently overwritten - which for consent evidence is worse than never
    having kept it, because nothing shows that it happened.

    First ask wins for both fields: a resend is the same question, and the date
    that matters is when they were first put on notice.
    """
    consent, _ = ChannelConsent.objects.get_or_create(
        participant=participant, channel=channel
    )

    fields = []
    if consent.asked_at is None:
        consent.asked_at = timezone.now()
        fields.append('asked_at')
    if wording and not consent.consent_text:
        consent.consent_text = wording
        fields.append('consent_text')
    if fields:
        consent.save(update_fields=fields)
    return consent


# How long an admin must wait before asking the same person again. A nudge is a
# message to somebody who has not consented, so the floor is a real protection
# rather than a nicety.
NUDGE_INTERVAL = timedelta(hours=24)


def nudge_refusal(participant, channel, *, now=None):
    """Why this member must not be nudged about `channel`, or None if they may.

    A string rather than a boolean, for the reason `refusal_reason` gives: the
    UI has to explain a disabled button, and "they said no" and "asked an hour
    ago" are very different things to tell an admin.

    NEVER after an answer. Somebody who replied NO has answered the question,
    and somebody who sent STOP has told the carrier. Asking either of them
    again is the behaviour that gets a campaign shut down, so both are refused
    here rather than left to the caller to remember.
    """
    consent = ChannelConsent.objects.filter(
        participant=participant, channel=channel
    ).first()
    refusal = standing_nudge_refusal(participant, channel, consent)
    if refusal is not None:
        return refusal

    # PENDING and asked, from here. `asked_at` is the first ask and never
    # moves, so the clock runs from whichever contact was most recent.
    now = now or timezone.now()
    waited = now - max(filter(None, (consent.asked_at, consent.reminded_at)))
    if waited < NUDGE_INTERVAL:
        remaining = NUDGE_INTERVAL - waited
        hours = int(remaining.total_seconds() // 3600) + 1
        return f'Asked recently. You can nudge again in about {hours}h.'

    return None


def standing_nudge_refusal(participant, channel, consent):
    """Why `nudge_refusal` would refuse however long they waited, or None.

    Everything but the once-a-day floor, which is the one refusal that passes
    by itself. Split out so the member list can say whether to offer a nudge at
    all without walking these rules again - a client that re-derived them from
    `status` is how a member added by phone came to be offered a nudge about
    email.

    `consent` is this member's row for `channel`, or None. Passed in rather
    than fetched, so a page of members reads the prefetch.
    """
    if participant.status != Participant.Status.ACTIVE:
        return 'They are not an active member.'

    # The reminder is a text: `nudge` sends it through send_notice, in SMS
    # wording. Nudging "about email" would text them instead, and past the SMS
    # row's floor, since the floor is kept per channel.
    if channel != ChannelConsent.Channel.SMS:
        return 'Only a text invitation can be sent again for now.'

    if consent is None:
        return 'They have never been asked about this channel.'

    if consent.status == ChannelConsent.Status.OPTED_IN:
        return 'They have already agreed.'
    if consent.status == ChannelConsent.Status.DECLINED:
        return 'They declined. Asking again is not ours to do.'
    if consent.status == ChannelConsent.Status.OPTED_OUT:
        return 'They opted out. Only they can undo that.'

    if consent.asked_at is None and consent.reminded_at is None:
        return 'Nothing has been sent to them yet.'

    # Without a number, send_notice finds nobody to text and returns quietly -
    # after `nudge` has stamped the reminder and told the admin it went out.
    from sms.outbound import number_for

    if not number_for(participant):
        return 'They have no number to text.'

    return None


def nudge(participant, channel, by, *, text=None):
    """Ask an unanswered invitation again. Admins only, once a day at most.

    Returns the ChannelConsent row. Raises JoinRefused with the reason when the
    member may not be nudged, so a caller cannot send by forgetting to check.

    `asked_at` and `consent_text` are left alone deliberately - they record the
    FIRST time somebody was put on notice and in what words, which is what a
    consent complaint turns on. Only `reminded_at` moves.
    """
    if not participant.conversation.may_add_members(by):
        raise JoinRefused('You cannot nudge people in this group.')

    refusal = nudge_refusal(participant, channel)
    if refusal is not None:
        raise JoinRefused(refusal)

    consent = ChannelConsent.objects.get(participant=participant, channel=channel)
    consent.reminded_at = timezone.now()
    consent.save(update_fields=['reminded_at', 'updated_at'])

    if text is None:
        conversation = participant.conversation
        text = (
            f'Reminder: you were added to {conversation.name} on LoopD. '
            'Reply YES to receive this group by text, NO to decline. '
            'STOP to never hear from us.'
        )

    # Through send_notice like the invitation it repeats, so the kill switch,
    # the spend ceilings and the delivery log all apply. A reminder that
    # bypassed them would be an unrecorded way to spend money on the one kind
    # of message that most needs a record.
    from sms.outbound import send_notice

    transaction.on_commit(
        lambda: send_notice(
            participant.conversation, participant, text, kind='invitation'
        )
    )
    return consent


def decline_email(participant):
    """Stop this group emailing this member. The one-way exit.

    Behind the unsubscribe link, the mail client's one-click POST, and a reply
    of STOP EMAILS - three doors into one room, so there is one place to get
    the rule right.

    Deliberately does NOT require an active membership, unlike every other
    self-service setter here. The link is printed into an email and lives in a
    mailbox for as long as the mail does, so it will be pressed by people who
    have since left, been removed, or been blocked. Recording their answer is
    correct and it survives a rejoin: `grant_email_consent_on_claim` only
    upgrades PENDING, so it will not overwrite this.

    Returns True when something actually changed, so a caller can be honest
    about a repeat without having to compare the row itself.
    """
    already = consent_status(participant, ChannelConsent.Channel.EMAIL)
    set_channel_consent(
        participant, ChannelConsent.Channel.EMAIL, ChannelConsent.Status.DECLINED
    )
    return already != ChannelConsent.Status.DECLINED


def set_group_email(participant, enabled):
    """The member's own switch for one group's email, from inside the app.

    The only door that turns email back ON, which is why this one does require
    an active membership and the unsubscribe link does not. Nothing reachable
    from an emailed token may add - a forwarded link must not let a stranger
    undo somebody's decision.

    It will not clear an OPTED_OUT. That state is the provider's verdict - a
    spam complaint the mailbox provider already acted on - and an app toggle
    must not overturn it, for the same reason `invite_by_phone` refuses to
    reset a STOP and only START may. Nothing writes email OPTED_OUT yet; the
    guard goes in now because the complaint webhook is the next piece of work
    and this is the rule the SMS side learned the hard way.
    """
    if participant.status != Participant.Status.ACTIVE:
        raise NotAMember('You are not a member of that conversation.')

    answer = consent_status(participant, ChannelConsent.Channel.EMAIL)
    if answer == ChannelConsent.Status.OPTED_OUT:
        return participant

    set_channel_consent(
        participant,
        ChannelConsent.Channel.EMAIL,
        ChannelConsent.Status.OPTED_IN if enabled
        else ChannelConsent.Status.DECLINED,
    )
    return participant


def grant_email_consent_on_claim(participant):
    """A row that has just gained an account may be emailed about this group.

    Called from the two paths that turn a phone-only or address-only row into a
    real membership. Both of them mean the same thing: the person reading that
    mailbox proved it is theirs, and being added to a group by an address you
    have verified is agreeing to hear from it - that is what the invitation
    said and what accepting it was.

    Deliberately NOT the same treatment as SMS, and the asymmetry is the point.
    Nobody has confirmed the phone number by text, so its answer stays PENDING
    until they reply YES. Signing up with an email address is not a reply to a
    question about a number.

    Only PENDING is upgraded. Somebody who declined this group, or who replied
    STOP, has answered - and verifying an address is not a way to overwrite it.
    """
    if participant.user_id is None:
        return None

    answer = consent_status(participant, ChannelConsent.Channel.EMAIL)
    if answer is not None and answer != ChannelConsent.Status.PENDING:
        return None

    return set_channel_consent(
        participant, ChannelConsent.Channel.EMAIL, ChannelConsent.Status.OPTED_IN
    )


def rekey_external_direct(conversation, user):
    """Re-file an external thread under its pair of accounts, once it has two.

    The address has just become an account, so the conversation is now an
    ordinary one-to-one - but it is still filed under `'<id>:email:<address>'`.
    Left that way, the next `start_direct` between the two computes the id-pair
    key, finds nothing, and opens a SECOND thread. That is exactly the
    duplicate `direct_key` exists to prevent, arriving by the back door.

    So the key is rewritten. Nothing else moves: same conversation, same
    messages, same participants - only its filing name changes.

    THE COLLISION IS REAL AND IS HANDLED. The pair may already have a thread,
    if they got an account some other way and were messaged there while the
    bridge also existed. The unique constraint refuses the rewrite, and the
    bridge is folded into the older thread instead - the same "one human, two
    rows, keep one and move the history" move `claim_email_invitations` makes
    one level down, applied a level up.

    Returns 'rekeyed', 'folded', or None when there was nothing to do. The
    caller needs to tell the first two apart: folding DELETES this conversation
    and the participant row with it, so anything that would go on to touch that
    row - announcing the membership, say - must not.

    Never raises: this runs inside email verification, which has already
    succeeded by the time it is reached.
    """
    if not conversation.is_external_direct:
        return None

    other = (
        Participant.objects.filter(conversation=conversation, user__isnull=False)
        .exclude(user=user)
        .select_related('user')
        .first()
    )
    if other is None:
        return None

    key = Conversation.direct_key_for(other.user, user)
    try:
        with transaction.atomic():
            conversation.direct_key = key
            conversation.save(update_fields=['direct_key', 'updated_at'])
        return 'rekeyed'
    except IntegrityError:
        pass

    existing = Conversation.objects.filter(direct_key=key).first()
    if existing is None or existing.pk == conversation.pk:
        logger.warning(
            'Could not re-key external conversation %s and found no thread to '
            'fold it into', conversation.pk,
        )
        return None

    try:
        with transaction.atomic():
            _fold_into(conversation, existing, user)
    except Exception:
        logger.exception(
            'Could not fold external conversation %s into %s',
            conversation.pk, existing.pk,
        )
        return None
    return 'folded'


def _fold_into(bridge, target, user):
    """Move one external thread's messages into the pair's existing one.

    `sender_participant` is SET_NULL, so the rows have to be re-pointed before
    the bridge goes - deleting first would orphan everything posted through it.
    """
    rows = {
        row.user_id: row
        for row in Participant.objects.filter(
            conversation=target, user__isnull=False
        )
    }
    for row in Participant.objects.filter(conversation=bridge):
        replacement = rows.get(row.user_id)
        if replacement is not None:
            row.messages_sent.update(sender_participant=replacement)

    bridge.messages.update(conversation=target)
    bridge.delete()
    logger.info('Folded external conversation into %s for user %s', target.pk, user.pk)


def consent_status(participant, channel):
    """This member's answer for one channel, or None if there is no record."""
    consent = ChannelConsent.objects.filter(
        participant=participant, channel=channel
    ).first()
    return consent.status if consent else None


def set_archived(participant, archived):
    """Tidy this conversation out of one member's list, or put it back.

    Not a departure and not a mute: they stay a member, the texts keep coming,
    and everyone else's list is untouched. It decides which of the member's own
    lists the conversation appears in and nothing else.
    """
    if participant.status != Participant.Status.ACTIVE:
        raise NotAMember('You are not a member of that conversation.')

    stamp = timezone.now() if archived else None
    if (participant.archived_at is None) == (stamp is None):
        return participant

    participant.archived_at = stamp
    participant.save(update_fields=['archived_at'])
    return participant


def unarchive_for_new_message(conversation):
    """Bring a conversation back into the lists it was tidied out of.

    Archive means "not now", not "never again" - so something happening puts it
    back, the same way an email thread returns to the inbox on a reply. A
    muted member is the exception and is left alone: they have said they do not
    want to hear about this group, and pulling it back to the top of their list
    every time somebody posts is precisely hearing about it.

    Returns the number of members it surfaced for, which is zero in the common
    case where nobody had archived it.
    """
    return Participant.objects.filter(
        conversation=conversation,
        status=Participant.Status.ACTIVE,
        archived_at__isnull=False,
        muted=False,
    ).update(archived_at=None)


def set_last_read(participant, message):
    """Move this member's read pointer up to `message`.

    Forward only. A tab that was backgrounded for a minute reports the message
    it had when it went to sleep, and honouring that would drag the pointer
    back and turn messages the member has already read unread again - on every
    device at once, because the pointer is the account's, not the browser's.
    So an older id is not an error, it is simply nothing to do.

    Returns True when the pointer actually moved, so the caller knows whether
    there is anything worth telling the member's other devices about.
    """
    if participant.status != Participant.Status.ACTIVE:
        raise NotAMember('You are not a member of that conversation.')

    if message.conversation_id != participant.conversation_id:
        raise NotAMember('That message is not in this conversation.')

    if message.pk <= (participant.last_read_message_id or 0):
        return False

    participant.last_read_message = message
    participant.save(update_fields=['last_read_message'])

    # To the member's OWN devices, so a badge cleared on the laptop clears on
    # the phone too. Deliberately not to the conversation: who has read what is
    # the member's business, and putting it on the group's socket would turn a
    # private pointer into a read receipt for everybody.
    #
    # After commit, so a client that reacts by refetching cannot race the write.
    from chat.services import broadcast_conversation_read

    conversation = participant.conversation
    user = participant.user
    if user is not None:
        transaction.on_commit(
            lambda: broadcast_conversation_read(conversation, user, message)
        )
    return True


def set_muted(participant, muted):
    """Silence this group for one member, or unsilence it.

    They stay a member: they can still read the history and post. Muting only
    stops delivery going out to them, which is what somebody texting MUTE is
    asking for.
    """
    if participant.status != Participant.Status.ACTIVE:
        raise NotAMember('You are not a member of that conversation.')
    participant.muted = muted
    participant.save(update_fields=['muted'])
    return participant


# --- internals -------------------------------------------------------------


def memberships_for(conversation, *, user=None, phone_number=None):
    """Every row in this conversation that belongs to this person.

    PURE IDENTITY. This answers "which rows are this human?" and nothing else -
    no status, no ban, no permission. That separation is load-bearing, and
    getting it wrong is how blocking fails OPEN: put `is_active` in here and a
    banned account stops being findable, so the caller concludes there is no
    existing row, creates a fresh one, and the person walks in. A ban must be
    enforced where the action is authorised, never where the person is
    identified.

    One human has up to two identifiers - an account and a phone number - and
    may hold a row under either. Matching only the one they happened to use is
    the other way a blocked member gets back in.

    The rule for trusting a number differs by row:

      row with no account   the number ON THE ROW is the identity. Nothing else
                            exists to check it against.
      row with an account   only the number on their PROFILE counts, and only
                            while verified. `Participant.phone_number` can be
                            left over from before they changed their number, and
                            a carrier reassigns a surrendered number to a
                            stranger within months.

    Queried through Participant.objects rather than `conversation.participants`:
    the viewset prefetches that relation filtered to ACTIVE, and a related
    manager filters the prefetch cache, so the relation cannot see the very rows
    this function exists to find.
    """
    condition = Q()
    matched = False

    if user is not None:
        condition |= Q(user=user)
        matched = True
        profile = getattr(user, 'profile', None)
        if profile is not None and profile.phone_no_verified and profile.phone_number:
            condition |= Q(user__isnull=True, phone_number=profile.phone_number)

    if phone_number:
        condition |= Q(user__isnull=True, phone_number=phone_number)
        condition |= Q(
            user__isnull=False,
            user__profile__phone_number=phone_number,
            user__profile__phone_no_verified=True,
        )
        matched = True

    if not matched:
        return Participant.objects.none()

    return (
        Participant.objects.filter(conversation=conversation)
        .filter(condition)
        .select_related('user__profile')
        .distinct()
    )


# BLOCKED first, always. Somebody holding a blocked row and an active one is
# blocked; any other order makes blocking advisory. Every caller that asks
# "which row represents this person" uses this one ordering - two answers to
# that question is precisely how a blocked member kept posting.
_STATUS_PRECEDENCE = (
    Participant.Status.BLOCKED,
    Participant.Status.ACTIVE,
    Participant.Status.PENDING,
)


def resolve(conversation, *, user=None, phone_number=None):
    """The one row that decides what happens to this person, or None."""
    rows = list(memberships_for(conversation, user=user, phone_number=phone_number))
    if not rows:
        return None

    by_status = {}
    for row in rows:
        by_status.setdefault(row.status, row)
    for status in _STATUS_PRECEDENCE:
        if status in by_status:
            return by_status[status]
    return rows[0]


def admin_users(conversation):
    """The accounts that administer a group.

    Phone-only rows cannot be admins and have no account to notify, so the
    join on `user` is what filters them out rather than a separate check.
    """
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(
        conversation_memberships__conversation=conversation,
        conversation_memberships__role=Participant.Role.ADMIN,
        conversation_memberships__status=Participant.Status.ACTIVE,
    ).distinct()


def _find_row(conversation, *, user=None, phone_number=None):
    return resolve(conversation, user=user, phone_number=phone_number)


def account_is_usable(participant):
    """False when this row's account has been banned.

    Checked at the point of action rather than inside the identity lookup, so
    that a banned person is still *found* - and therefore still blocked, still
    counted, still refused - rather than silently becoming a stranger who can
    make a brand new membership.
    """
    if participant is None or participant.user_id is None:
        return True
    return participant.user.is_active


def _code_matches(conversation, code):
    if not conversation.join_code or not code:
        return False
    # Codes are read off a screen and typed into a text message, so case and
    # surrounding whitespace are noise, not signal.
    return str(code).strip().upper() == conversation.join_code.upper()


def _tell_them(participant, text):
    """Text somebody with no account about their own membership.

    Only for people who have no other way of finding out. Somebody with an
    account learns it from the app - they get the conversation over their
    socket, or simply see it in their list - and texting them as well is a
    charge for something they already know.
    """
    if participant.user_id is not None:
        return
    from sms.outbound import send_notice

    conversation = participant.conversation
    transaction.on_commit(lambda: send_notice(conversation, participant, text))


def _announce_membership(participant):
    """Tell a live socket it now belongs to this conversation.

    Subscription is decided once, at connect. Somebody who joins with a code or
    is approved while the app is open is ACTIVE in the database and visible in
    their conversation list, but their socket was never subscribed - so the
    group sits there silent until they reload. The mirror image of
    `_revoke_socket`.
    """
    if participant.user_id is None or participant.status != Participant.Status.ACTIVE:
        return
    from chat.services import announce_added_to_conversation

    announce_added_to_conversation(participant.conversation, [participant.user])


def _revoke_socket(participant):
    """Drop this membership from any socket the member currently holds.

    After commit, not before: the consumer re-reads nothing, but a client that
    reacts by refetching must not race the transaction. Phone-only members have
    no socket, so there is nothing to do for them.
    """
    if participant.user_id is None:
        return
    from chat.services import broadcast_removed_from_conversation

    conversation = participant.conversation
    user = participant.user
    transaction.on_commit(
        lambda: broadcast_removed_from_conversation(conversation, [user])
    )


def _locked(participant):
    """Re-read a row inside the transaction, locked against concurrent writes.

    SQLite ignores select_for_update, which is fine: it serialises writers
    anyway, and production is Postgres.
    """
    return Participant.objects.select_for_update().get(pk=participant.pk)


def _admin_count(conversation, lock=False):
    """How many admins this group has right now.

    `lock=True` takes a row lock on them, so two admins leaving at the same
    instant cannot both read 2, both pass the guard and both write LEFT - which
    strands the group with nobody who can promote anyone.
    """
    queryset = Participant.objects.filter(
        conversation=conversation,
        status=Participant.Status.ACTIVE,
        role=Participant.Role.ADMIN,
    )
    if lock:
        queryset = queryset.select_for_update()
    return queryset.count()


def _refuse_blocked_invite(inviter, invitee=None, *, phone_number=None):
    """Refuse an invitation between two people in a block relationship.

    §11 of the blocking spec: B must not be able to put A into a group. It is
    the last direct channel left once messages are refused, and without this
    the block is a detour rather than a wall.

    Symmetric, like every other read of the rule - if A blocked B then B cannot
    add A, and A cannot add B either. One-way would be observable.

    Raises the same JoinRefused any other unaddable person raises, with wording
    that names nothing. A distinct message, or a distinct exception with its own
    status code, would be exactly the reveal §11 asks us to avoid: `loopd/
    exceptions.py` maps BlockedFromConversation to 403 and JoinRefused to 400,
    so using the former here would announce the block by status code alone.

    Group MEMBERSHIP is untouched by a block, so this refuses the invitation
    and never the group: if the two are already in a group together they stay
    there, and either may still join one the other is in by their own choice.
    """
    if inviter is None:
        return

    from user.blocks import blocks_between, blocks_number

    if phone_number is not None:
        if blocks_number(inviter, phone_number):
            raise JoinRefused('Unable to add this person.')

        # And the same number may BE an account. A block is recorded against
        # whichever identity the blocker named, but the person is one person -
        # so blocking the account and then being invited by number would have
        # been a way straight back in, on the identity that was not named.
        #
        # Only a VERIFIED number resolves, for the reason it never resolves
        # anywhere else: an unverified one is a string somebody typed into
        # their own profile, and honouring it would let a squatter make
        # themselves unaddressable by claiming a stranger's number.
        from user.models import Profile

        owner = (
            Profile.objects.filter(
                phone_number=phone_number, phone_no_verified=True
            )
            .select_related('user')
            .first()
        )
        if owner is not None and blocks_between(inviter, owner.user):
            raise JoinRefused('Unable to add this person.')
        return

    if blocks_between(inviter, invitee):
        raise JoinRefused('Unable to add this person.')


def _require_admin(conversation, by):
    if by is None or not conversation.is_admin(by):
        raise JoinRefused('Only a group admin can do that.')


def invite_by_email(conversation, email, by):
    """Add somebody to a group by their email address.

    Two outcomes, and which one happens is decided by the address, not by the
    caller:

      THE ADDRESS BELONGS TO A VERIFIED ACCOUNT - they are added outright, the
      same as being named by username. `find_user` requires the address to be
      verified, so this can never hand a group to somebody who merely typed
      that address into their profile.

      NOBODY HAS IT - a row is created against the address and an invitation is
      emailed. It becomes real membership when they verify that address, in
      `claim_email_invitations`. Until then they are in no group they can see.

    Returns the Participant, or None for an address whose owner hid it in
    Settings → Privacy: nothing is added and nothing is emailed. Silence
    rather than a refusal, because any other answer would say that the address
    has an account behind it.
    """
    from user import privacy
    from user.lookups import find_user

    if not conversation.may_add_members(by):
        raise JoinRefused('You cannot add people to this group.')

    if conversation.kind != Conversation.Kind.GROUP:
        raise JoinRefused('Only groups have members to invite.')

    address = (email or '').strip().lower()
    if not address:
        raise JoinRefused('That is not an email address.')

    # No viewer passed, deliberately - see user/lookups.py. A block must not
    # make this return None, because None means "nobody has this address" and
    # sends them an invitation email. The refusal is `_refuse_blocked_invite`
    # below, where it can actually refuse.
    owner = find_user(address)
    if owner is not None:
        _refuse_blocked_invite(by, owner)
        if not privacy.allows_group_add(owner, by):
            raise JoinRefused(privacy.group_add_refusal([owner]))
        # A real account. add_participant carries the blocked checks and the
        # phone-row folding, so there is nothing to duplicate here.
        return conversation.add_participant(owner)

    # Nobody the lookup would return - but the address may still belong to
    # somebody who turned "Find me using my email" off. Emailing them an
    # invitation would put mail in their inbox for a group they could never
    # join: claiming happens on verifying the address, and theirs is verified
    # already. So nothing is created and nothing is sent.
    if find_user(address, respect_discoverability=False) is not None:
        return None

    existing = Participant.objects.filter(
        conversation=conversation, email__iexact=address
    ).first()
    if existing is not None and existing.status == Participant.Status.BLOCKED:
        raise BlockedFromConversation('That person is blocked from this group.')

    participant = existing or Participant.objects.create(
        conversation=conversation,
        email=address,
        display_name=local_part(address),
        added_by=by,
        role=Participant.Role.MEMBER,
    )

    # Re-inviting someone who has not claimed their row sends the invitation
    # again, which is the only way to recover from a bounced first attempt.
    transaction.on_commit(
        lambda: _email_invitation(conversation, participant, by)
    )
    return participant


def _email_invitation(conversation, participant, by):
    """Send one group invitation. Never raises.

    An invitation that cannot be sent must not undo a group that is already
    committed - and this runs in an on_commit callback, where an exception
    becomes a 500 on a request that already succeeded.

    TWO WAYS IN, ONE EMAIL. Reply YES and the group's messages arrive by email,
    where replying posts to the group - no account needed. Or sign up with this
    address and they are in the app. The first only exists where replies can
    reach us: with no reply domain there is nothing to reply TO, and the email
    offers the link alone rather than a YES that lands nowhere.

    It is a consent request, so it carries what the first contact of the
    one-to-one bridge carries: a reply address that names this membership, the
    unsubscribe link and its one-click header, and a record of exactly what
    they were asked - stamped only once the send succeeded, since a question
    that never arrived was not asked.
    """
    from django.template.loader import render_to_string

    from mail import commands
    from mail.outbound import _headers_for, from_header, reply_to_header
    from mail.tokens import unsubscribe_url
    from utils.mail import GROUP_INVITATION, EmailError, send_templated_email

    address = participant.email
    if not address:
        return

    inviter = getattr(getattr(by, 'profile', None), 'display_label', None)
    reply_to = reply_to_header(participant, conversation)
    stop_url = unsubscribe_url(participant)
    context = {
        'group_name': conversation.name,
        'inviter_name': inviter or 'Somebody',
        # Deliberately not a token. Membership in the app is granted by
        # verifying the address, so this only needs to carry them to the
        # right form with the right address in it.
        'join_url': f'{settings.FRONTEND_URL}/signup?email={quote(address)}',
        'can_reply': bool(reply_to),
        'stop_keyword': commands.KEYWORD,
        'unsubscribe_url': stop_url,
        'product_name': settings.EMAIL_PRODUCT_NAME,
        'support_address': settings.EMAIL_SUPPORT_ADDRESS,
    }

    try:
        send_templated_email(
            GROUP_INVITATION,
            to=address,
            context=context,
            from_email=from_header(conversation),
            reply_to=reply_to,
            headers=_headers_for(conversation, stop_url),
        )
    except EmailError:
        logger.exception(
            'Could not email the invitation for conversation %s', conversation.pk
        )
        return

    if reply_to:
        # The plain-text part, word for word: "what exactly were they asked"
        # is the question a complaint turns on, and the template will have
        # changed by the time anybody asks it.
        record_consent_asked(
            participant,
            ChannelConsent.Channel.EMAIL,
            render_to_string(GROUP_INVITATION.text_template, context).strip(),
        )


def claim_email_invitations(user):
    """Fold every pending email invitation for this user into their account.

    Called when an address is VERIFIED, which is the whole security model for
    email invitations: the row is created against an address, and only proving
    you read that mailbox turns it into membership. Nothing in the invitation
    email is a credential, so forwarding it gives the recipient nothing.

    The mirror of `Conversation._claim_phone_participant`, with one deliberate
    difference: this never raises. It runs inside email verification, and a
    blocked row in one group must not be able to stop somebody verifying their
    account. Blocked rows are skipped and stay blocked - registering against a
    blocked address is not a way back in.

    Returns the number of rows claimed.
    """
    profile = getattr(user, 'profile', None)
    address = (user.email or '').strip()
    if profile is None or not profile.email_verified or not address:
        return 0

    pending = Participant.objects.filter(
        user__isnull=True, email__iexact=address
    ).select_related('conversation')

    from user.blocks import blocks_between

    claimed = 0
    for row in pending:
        if row.status == Participant.Status.BLOCKED:
            continue

        # The other half of the invite refusal, and the half that catches the
        # case `invite_by_email` structurally cannot.
        #
        # A block is keyed on an account or a number, never on a bare address -
        # and an UNVERIFIED address resolves to nobody, on purpose, because
        # anybody can type somebody else's address into their own profile. So
        # if B invites A by an address A has not verified yet, `find_user`
        # returns None, no block can be matched, and a row is created.
        #
        # This is the moment that row would become a real membership, and it is
        # the first moment the address has an owner at all. Refusing here rather
        # than matching unverified addresses earlier also closes a griefing
        # route: otherwise somebody could put a stranger's address on their own
        # profile, block the inviter, and stop that stranger being invited.
        #
        # The row is left behind rather than deleted, exactly like a blocked
        # one: it is dormant, it grants nothing, and deleting it would let a
        # second invitation quietly create a fresh one.
        if row.added_by_id and blocks_between(user, row.added_by):
            logger.info(
                'Not claiming invitation %s: blocked between %s and %s',
                row.pk, user.pk, row.added_by_id,
            )
            continue

        # And their own "Who can add me to groups", read at the same moment
        # and for the same reason: until now this row was an address, and an
        # address has no settings to ask about.
        from user import privacy

        if row.added_by_id and not privacy.allows_group_add(user, row.added_by):
            logger.info(
                'Not claiming invitation %s: %s does not accept group adds from %s',
                row.pk, user.pk, row.added_by_id,
            )
            continue

        existing = Participant.objects.filter(
            conversation_id=row.conversation_id, user=user
        ).first()

        if existing is None:
            # The address has done its job; the account carries it from here,
            # and clearing it frees the slot for a future invitation.
            row.user = user
            row.email = None
            row.status = Participant.Status.ACTIVE
            row.save(update_fields=['user', 'email', 'status'])
            grant_email_consent_on_claim(row)
            outcome = rekey_external_direct(row.conversation, user)
            claimed += 1
            if outcome != 'folded':
                # Folding deleted this row along with its conversation, so
                # there is no membership left to announce - and the pair were
                # already talking in the thread it was folded into.
                _announce_membership(row)
            continue

        # Already in as an account and as an address: one human, two rows.
        # Keep the account row and move the history across before dropping the
        # duplicate - `sender_participant` is SET_NULL, so deleting first would
        # orphan anything they posted.
        row.messages_sent.update(sender_participant=existing)
        row.delete()

        if existing.status == Participant.Status.BLOCKED:
            continue
        if existing.status != Participant.Status.ACTIVE:
            existing.status = Participant.Status.ACTIVE
            # Coming back in on THIS invitation, so what it allowed them to
            # read - "Only show new messages", or everything - comes with it.
            existing.history_after_message_id = row.history_after_message_id
            existing.save(update_fields=['status', 'history_after_message_id'])
        claimed += 1
        _announce_membership(existing)

    if claimed:
        logger.info('Claimed %s email invitation(s) for user %s', claimed, user.pk)
    return claimed


def invite_by_phone(conversation, phone_number, display_name, by):
    """Add somebody to a group by their number, and ask them if that is alright.

    This is what makes Invite Only a usable mode: without it there is no way in
    at all, because JOIN is refused and nothing else adds a member.

    Consent starts PENDING and an invitation is texted. Nothing from the group
    reaches them until they reply YES - somebody else putting your number into a
    group is not you agreeing to be texted, and treating it as though it were is
    how a product ends up sending unsolicited messages on an admin's word alone.
    """
    from sms.outbound import send_notice

    # `may_add_members`, not `_require_admin`: a group whose
    # add_member_permission is MEMBERS lets any member bring somebody in, and
    # inviting by number is adding somebody.
    if not conversation.may_add_members(by):
        raise JoinRefused('You cannot add people to this group.')

    if conversation.kind != Conversation.Kind.GROUP:
        raise JoinRefused('Only groups have members to invite.')

    _refuse_blocked_invite(by, phone_number=phone_number)

    existing = _find_row(conversation, phone_number=phone_number)
    if existing is not None and existing.status == Participant.Status.BLOCKED:
        raise BlockedFromConversation('That person is blocked from this group.')

    participant = conversation.add_phone_participant(
        phone_number, display_name=display_name, added_by=by
    )

    answer = consent_status(participant, ChannelConsent.Channel.SMS)
    if answer is not None and answer != ChannelConsent.Status.PENDING:
        # They have already answered this group's question - YES, NO, or a
        # carrier-level STOP - and re-inviting is not a new question. Only a
        # PENDING invitation is sent again, because a pending one is the only
        # kind that may simply never have arrived.
        #
        # Preserving OPTED_IN alone was not enough. Resetting anything else to
        # PENDING made "no" mean "not yet": whoever added the member next
        # cleared the decline and texted them again, and did the same to a
        # number that had replied STOP - the one thing a carrier audit tests
        # for, and not something an admin's word should be able to undo.
        #
        # Both are recoverable, by the member and only by the member: YES
        # turns a declined group back on, START undoes a STOP.
        logger.info(
            'Not re-asking participant %s for SMS consent; already %s',
            participant.pk, answer,
        )
        return participant

    inviter = getattr(getattr(by, 'profile', None), 'display_label', None) or 'Somebody'
    text = (
        f'{inviter} added you to {conversation.name} on LoopD. '
        'Reply YES to receive this group by text, NO to decline. '
        'STOP to never hear from us.'
    )

    set_channel_consent(
        participant, ChannelConsent.Channel.SMS, ChannelConsent.Status.PENDING
    )
    # The exact words they were shown, on the SMS consent row. A complaint
    # turns on what was actually sent, and the template will have changed by
    # the time anyone asks.
    record_consent_asked(participant, ChannelConsent.Channel.SMS, text)

    transaction.on_commit(
        lambda: send_notice(conversation, participant, text, kind='invitation')
    )
    return participant
