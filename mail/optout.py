"""Telling somebody their email really did stop.

CONFIRM IN THE CHANNEL THE REQUEST ARRIVED ON
---------------------------------------------
    replied STOP EMAILS   ->  one email back
    pressed the link      ->  the page they are already looking at
    app or profile toggle ->  the UI they are already looking at

Only the first needs anything here, and only the first gets it. A confirmation
email sent to somebody who just PRESSED unsubscribe reads as being ignored, and
that is the click that becomes a spam complaint - which is the one event that
really does land an address on the provider's account-wide suppression list.

This is also where email departs from SMS deliberately. `sms/inbound.py` answers
STOP with silence, because the carrier and the messaging profile already send
the mandated confirmation and a second one is a double-ack and a second charge.
Email has no such ack. A member who replies and hears nothing does not know it
worked, and the next thing they do is press Report spam.
"""
import logging

from django.conf import settings

from mail.models import EmailDelivery
from mail.outbound import _hash_address, address_for, from_header
from utils.mail import UNSUBSCRIBED, EmailError, send_templated_email

logger = logging.getLogger(__name__)


def send_confirmation(participant):
    """Tell one member their email for one group has stopped. Never raises.

    Runs from `transaction.on_commit`, where Django executes callbacks on the
    request thread AFTER the commit - so an exception here becomes a 500 on a
    webhook that has already succeeded, and a provider reads a 500 as "send it
    again". The reply would arrive back, be recognised, and be confirmed again.
    """
    address = address_for(participant)
    if not address:
        return None

    conversation = participant.conversation
    record = EmailDelivery.objects.create(
        direction=EmailDelivery.Direction.OUTBOUND,
        conversation=conversation,
        # No message: this is about the conversation, not part of it. The
        # unique constraint is on (message, participant) for outbound rows and
        # NULL never equals NULL, so these never collide with each other or
        # with a fan-out row.
        message=None,
        participant=participant,
        user=participant.user,
        counterpart_domain=address.rpartition('@')[2][:64],
        counterpart_hash=_hash_address(address),
        status=EmailDelivery.Status.SENDING,
    )

    try:
        message_id = send_templated_email(
            UNSUBSCRIBED,
            to=address,
            context={
                'group_name': conversation.name,
                'app_url': settings.FRONTEND_URL,
            },
            from_email=from_header(conversation),
            # RFC 3834, and it matters more here than on a group message: this
            # is the last mail they will get from this group, and an
            # out-of-office bouncing off it would be answered by nothing.
            headers={'Auto-Submitted': 'auto-generated'},
            # Deliberately NO List-Unsubscribe. They have just unsubscribed;
            # offering it again would be absurd, and the header belongs only on
            # mail somebody can still opt out of.
            idempotency_key=f'unsub-p-{participant.pk}',
        )
    except EmailError as exc:
        # Their preference is already saved - that happened before this was
        # scheduled. Failing to say so is worth a log line and nothing more.
        record.status = EmailDelivery.Status.FAILED
        record.reason = str(exc)[:120]
        record.save(update_fields=['status', 'reason', 'updated_at'])
        logger.warning(
            'Could not confirm the email opt-out for participant %s', participant.pk
        )
        return record

    record.status = EmailDelivery.Status.SENT
    record.provider = 'resend' if message_id else ''
    record.provider_message_id = message_id or ''
    record.save(
        update_fields=['status', 'provider', 'provider_message_id', 'updated_at']
    )
    return record
