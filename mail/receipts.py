"""What the provider tells us afterwards about mail we sent.

An email leaves through `mail/outbound.py` as SENT, and that is the last the
sending code knows. The rest of the story arrives later, one webhook event at
a time: it was delivered, it bounced, the person marked it as spam. Until this
module existed those events were answered 200 and thrown away, so the delivery
log stopped at "sent" and the product kept mailing addresses that had told it,
in the only ways an address can, to stop.

TWO OF THESE CHANGE THE ACCOUNT, NOT JUST THE ROW
--------------------------------------------------
A COMPLAINT is the person saying stop, in the one place that also tells their
mail provider we are spam. Group email is switched off for their whole account -
`MessagingSettings.email_enabled`, the same switch Settings > Messaging shows - so
they see it off and can turn it back on themselves if it was a slip. Nothing
else would do: the next message would produce the next complaint, and a
domain's reputation does not come back.

A PERMANENT BOUNCE means the mailbox is not there. The provider puts the
address on its suppression list either way; switching the account off as well
means our own log says why nothing is arriving, and the person sees it in
their settings rather than wondering.

A temporary bounce changes only the row. The provider already retried; a
second copy from us on top would be the wrong kind of persistence.
"""
import logging

from django.utils import timezone

from mail import provider
from mail.models import EmailDelivery
from user.models import MessagingSettings

logger = logging.getLogger(__name__)


def record(event):
    """Apply one provider event to the rows it is about. Returns rows touched.

    Keyed on the id the provider returned when we sent, which `_deliver` stored
    as `provider_message_id`. An id we do not know is answered by doing
    nothing: a receipt for a verification code, or for mail sent from the
    dashboard by hand.
    """
    if not event.email_id:
        return 0

    rows = EmailDelivery.objects.filter(
        direction=EmailDelivery.Direction.OUTBOUND,
        provider_message_id=event.email_id,
    )
    now = timezone.now()

    if event.kind == provider.DELIVERED:
        return rows.update(
            status=EmailDelivery.Status.DELIVERED, delivered_at=now, updated_at=now
        )

    if event.kind == provider.BOUNCED:
        touched = rows.update(
            status=EmailDelivery.Status.BOUNCED,
            reason=_reason('bounced', event),
            updated_at=now,
        )
        if event.permanent:
            _stop_mailing(
                rows, 'the address bounced permanently',
                MessagingSettings.EmailOptOutReason.BOUNCE,
            )
        return touched

    if event.kind == provider.COMPLAINED:
        touched = rows.update(
            status=EmailDelivery.Status.COMPLAINED,
            reason=_reason('marked as spam', event),
            updated_at=now,
        )
        _stop_mailing(
            rows, 'they marked a group email as spam',
            MessagingSettings.EmailOptOutReason.COMPLAINT,
        )
        return touched

    if event.kind == provider.SUPPRESSED:
        # Never left the provider. Permanent for the retry sweep: the address
        # is on a list we do not control, and asking again asks the same list.
        return rows.update(
            status=EmailDelivery.Status.FAILED,
            reason='suppressed by the provider',
            updated_at=now,
        )

    if event.kind == provider.FAILED:
        # Accepted by the API, failed afterwards - a quota, usually. Left as
        # FAILED with the provider's word for it, so the sweep tries again.
        return rows.update(
            status=EmailDelivery.Status.FAILED,
            reason=_reason('provider failed', event),
            updated_at=now,
        )

    return 0


def _opt_out_account_less(rows, why):
    """OPTED_OUT for every address-only recipient among these rows.

    OPTED_OUT rather than DECLINED on purpose: a complaint or a hard bounce is
    about this address everywhere, not about one conversation, and it is the
    one state a later YES may never clear.
    """
    from chat.membership import set_channel_consent
    from chat.models import ChannelConsent, Participant

    participant_ids = {
        row_id
        for row_id in rows.values_list('participant_id', flat=True)
        if row_id
    }
    if not participant_ids:
        return

    stopped = 0
    for participant in Participant.objects.filter(
        pk__in=participant_ids, user__isnull=True
    ):
        set_channel_consent(
            participant,
            ChannelConsent.Channel.EMAIL,
            ChannelConsent.Status.OPTED_OUT,
        )
        stopped += 1

    if stopped:
        logger.warning(
            'Opted %s address-only member(s) out of email: %s', stopped, why
        )


def _stop_mailing(rows, why, reason):
    """Switch email off for everybody these rows were sent to.

    With the record of when and why (`MessagingSettings.email_off`). Only rows
    still on are touched, so an account already off keeps the reason it was
    first turned off for.

    A recipient with no account has no MessagingSettings to switch, so the
    equivalent is written where their answer lives - `ChannelConsent`, as
    OPTED_OUT. Without this a bounce or a spam complaint against an address-only
    member changed nothing at all and the conversation kept emailing them,
    which is precisely the event that gets a sending domain suppressed.
    """
    _opt_out_account_less(rows, why)

    user_ids = {user_id for user_id in rows.values_list('user_id', flat=True) if user_id}
    if not user_ids:
        return
    off = MessagingSettings.email_off(reason)
    switched = MessagingSettings.objects.filter(
        user_id__in=user_ids, email_enabled=True
    ).update(**off, updated_at=off['email_opt_out_at'])

    # Every account has a row, but the opt-out must stick even for one that
    # somehow does not - so create it, already off.
    missing = user_ids - set(
        MessagingSettings.objects.filter(user_id__in=user_ids).values_list(
            'user_id', flat=True
        )
    )
    if missing:
        MessagingSettings.objects.bulk_create(
            [MessagingSettings(user_id=user_id, **off) for user_id in missing],
            ignore_conflicts=True,
        )
        switched += len(missing)
    if switched:
        logger.warning(
            'Switched group email off for %s account(s): %s', switched, why
        )


def _reason(label, event):
    detail = (event.detail or '').strip()
    return (f'{label}: {detail}' if detail else label)[:120]
