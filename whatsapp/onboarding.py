"""Connecting a WhatsApp Business number, and attaching it to groups.

OPTIONAL, AND NEVER PART OF SIGNUP
----------------------------------
Nothing here runs when somebody registers or logs in. A person connects
WhatsApp deliberately, from one of two places in the web app:

    Profile settings     "Connect WhatsApp Business"
    A group's members    "Invite by WhatsApp" - which sends an owner with no
                         connection through the same flow first

Both run Meta's Embedded Signup in the browser and end at `connect` below.

THE FLOW
--------
    browser   FB.login(config_id=...)  ->  code
              session-info message     ->  waba_id, phone_number_id
    POST /api/v1/whatsapp/connections/ {code, waba_id, phone_number_id}
    server    exchange code for the business token
              ask Meta which WABAs that token covers       <- ownership check
              check the phone number belongs to that WABA  <- ownership check
              subscribe our app to the WABA's webhooks
              register the number for Cloud API
              read its display number and verified name
              store it, token encrypted

The two ownership checks are the security of this whole feature. The WABA and
number ids come from the browser, so they are claims; only the token is proof.
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from chat.models import Conversation
from whatsapp import graph
from whatsapp.errors import ConnectionRefused
from whatsapp.graph import WhatsAppError
from whatsapp.models import GroupChannel, WhatsAppConnection

logger = logging.getLogger(__name__)


def connect(user, *, code, waba_id, phone_number_id, business_id=''):
    """Finish Embedded Signup for `user`. Returns the WhatsAppConnection."""
    waba_id, phone_number_id = str(waba_id).strip(), str(phone_number_id).strip()

    exchanged = graph.exchange_code(code)
    token = exchanged['access_token']

    granted = graph.granted_waba_ids(token)
    if granted is not None and waba_id not in granted:
        # Not a user error worth explaining in detail: either the browser was
        # tampered with or the flow was completed for a different account.
        logger.warning('Embedded Signup token does not cover WABA %s (user %s)',
                       waba_id, user.pk)
        raise ConnectionRefused(
            'That WhatsApp Business account was not shared with Loopd\'n.',
            code='waba_not_granted',
        )

    if phone_number_id not in graph.phone_numbers_of(waba_id, token):
        raise ConnectionRefused(
            'That phone number does not belong to the WhatsApp Business account.',
            code='number_not_in_waba',
        )

    live = WhatsAppConnection.objects.filter(
        phone_number_id=phone_number_id
    ).exclude(status=WhatsAppConnection.Status.DISCONNECTED).first()
    if live is not None and live.owner_id != user.pk:
        raise ConnectionRefused(
            'That number is already connected to another Loopd\'n account.',
            code='number_in_use',
        )
    if live is not None and live.status == WhatsAppConnection.Status.SUSPENDED:
        raise ConnectionRefused(
            'That number has been suspended. Contact support.', code='suspended'
        )

    graph.subscribe_app(waba_id, token)
    graph.register_number(phone_number_id, token)
    details = graph.phone_number_details(phone_number_id, token)

    connection = live or WhatsAppConnection(owner=user, phone_number_id=phone_number_id)
    connection.waba_id = waba_id
    connection.business_id = str(business_id or '')[:64]
    connection.status = WhatsAppConnection.Status.ACTIVE
    connection.disconnected_at = None
    connection.last_error = ''
    connection.set_access_token(token)
    if exchanged.get('expires_in'):
        connection.token_expires_at = timezone.now() + timedelta(
            seconds=int(exchanged['expires_in'])
        )
    _apply_details(connection, details)

    try:
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        # Two tabs finishing the flow at once for the same number.
        raise ConnectionRefused(
            'That number is already connected to another Loopd\'n account.',
            code='number_in_use',
        )
    logger.info('WhatsApp number %s connected by user %s', phone_number_id, user.pk)
    return connection


def refresh(connection):
    """Re-read the number's name, quality and limits from Meta."""
    try:
        details = graph.phone_number_details(
            connection.phone_number_id, connection.access_token
        )
    except WhatsAppError as exc:
        if exc.permanent:
            mark_broken(connection, str(exc))
        raise
    _apply_details(connection, details)
    connection.save()
    return connection


def disconnect(connection, *, reason=''):
    """Stop using this number. Its groups stop carrying WhatsApp immediately.

    The GroupChannel rows are deleted rather than left pointing at a dead
    connection: reconnecting later is a fresh decision about which groups it
    should carry. Unsubscribing from Meta is best effort - the owner may have
    already removed us in Business Manager, which is one way we got here.
    """
    if connection.status == WhatsAppConnection.Status.ACTIVE and reason != 'removed_by_meta':
        try:
            graph.unsubscribe_app(connection.waba_id, connection.access_token)
        except Exception:
            logger.warning('Could not unsubscribe from WABA %s', connection.waba_id)

    with transaction.atomic():
        connection.group_channels.all().delete()
        connection.status = WhatsAppConnection.Status.DISCONNECTED
        connection.disconnected_at = timezone.now()
        connection.last_error = reason[:255]
        # Nothing may send from a disconnected number, so nothing needs its key.
        connection.set_access_token('')
        connection.save()
    return connection


def mark_broken(connection, reason):
    """Meta says the credential or the number no longer works."""
    WhatsAppConnection.objects.filter(
        pk=connection.pk, status=WhatsAppConnection.Status.ACTIVE
    ).update(
        status=WhatsAppConnection.Status.ERROR,
        last_error=reason[:255],
        updated_at=timezone.now(),
    )
    logger.error('WhatsApp connection %s marked broken: %s', connection.pk, reason)


def _apply_details(connection, details):
    connection.display_phone_number = str(details.get('display_phone_number', ''))[:32]
    connection.verified_name = str(details.get('verified_name', ''))[:128]
    connection.quality_rating = str(details.get('quality_rating', ''))[:16]
    connection.messaging_limit_tier = str(details.get('messaging_limit_tier', ''))[:32]


# --- attaching a connection to a group ---------------------------------------


def attach(conversation, connection, by, *, keyword=''):
    """Carry this group over this number. Returns the GroupChannel.

    Two people have to agree, and here they are the same person: the admin of
    the group, and the owner of the number. An admin may not put their group on
    somebody else's business number, and a number's owner may not push their
    number onto a group they do not run.
    """
    if conversation.kind != Conversation.Kind.GROUP:
        raise ConnectionRefused('Only groups can be connected to WhatsApp.')
    if not conversation.is_admin(by):
        raise ConnectionRefused('Only a group admin can connect WhatsApp.', code='not_admin')
    if connection.owner_id != by.pk:
        raise ConnectionRefused('That WhatsApp number is not yours.', code='not_owner')
    if not connection.is_active:
        raise ConnectionRefused(
            'That WhatsApp number is not connected. Reconnect it first.',
            code='whatsapp_not_connected',
        )

    keyword = _clean_keyword(keyword)
    existing = GroupChannel.objects.filter(
        conversation=conversation, kind=GroupChannel.Kind.WHATSAPP
    ).first()

    if existing is not None and existing.whatsapp_connection_id == connection.pk:
        if keyword and keyword != existing.keyword:
            _ensure_keyword_free(connection, keyword, exclude=existing.pk)
            existing.keyword = keyword
            existing.save(update_fields=['keyword', 'updated_at'])
        return existing

    if keyword:
        _ensure_keyword_free(connection, keyword)
    else:
        keyword = GroupChannel.unused_keyword(connection, conversation.name)

    with transaction.atomic():
        if existing is not None:
            # Moving the group to a different number of the same admin.
            existing.delete()
        return GroupChannel.objects.create(
            conversation=conversation,
            whatsapp_connection=connection,
            keyword=keyword,
            created_by=by,
        )


def detach(channel, by):
    conversation = channel.conversation
    if not (conversation.is_admin(by) or channel.whatsapp_connection.owner_id == by.pk):
        raise ConnectionRefused('Only a group admin can disconnect WhatsApp.', code='not_admin')
    channel.delete()


def _clean_keyword(keyword):
    from whatsapp.models import keyword_from

    return keyword_from(keyword)


def _ensure_keyword_free(connection, keyword, exclude=None):
    taken = GroupChannel.objects.filter(whatsapp_connection=connection, keyword=keyword)
    if exclude is not None:
        taken = taken.exclude(pk=exclude)
    if taken.exists():
        raise ConnectionRefused(
            'Another group on this number already uses that keyword.', code='keyword_taken'
        )
