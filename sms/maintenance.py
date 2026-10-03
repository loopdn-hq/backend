"""The jobs that keep the SMS layer honest while nobody is watching.

Everything here answers a question the product cannot otherwise answer:

    retry_failed_deliveries    did anybody miss a message because a carrier had
                               a bad minute?
    requeue_stalled_deliveries did a worker die mid-send and leave a row that
                               will sit in SENDING forever?
    provision_missing_numbers  is there a group that never got a number because
                               the provider was down when it was created?
    purge_old_deliveries       is the log growing without limit?
    summarize_usage            what did each group and account use, month by
                               month, long after the detail is gone?
    reconcile_verification_texts
                               what did each sign-up code text cost, and did
                               it arrive?
    daily_spend_report         what did yesterday cost, and did anything hit a
                               ceiling?

Plain functions, called by three-line tasks in sms/tasks.py, so every one of
them is testable by calling it - no broker, no beat, no Celery in the test
suite at all.
"""
import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from chat import delivery as delivery_rules
from chat.models import Conversation, Message
from sms.models import (
    GroupNumber,
    NumberLease,
    NumberRentalMonth,
    SmsDelivery,
    SmsUsageMonth,
    SmsWebhookEvent,
    VerificationText,
)

logger = logging.getLogger(__name__)

# Reasons that will fail the same way every time. Retrying these is a standing
# charge for a message that is never going to arrive. The ladder's own
# decisions come from chat/delivery.py; the rest are SMS's.
PERMANENT_REASONS = delivery_rules.POLICY_REASONS + (
    'destination not allowed',
    'no verified number',
    'no number',
    'sms disabled for this group',
    'sms disabled account-wide',
    'group is too large to text',
    'sending is switched off',
)


def retry_failed_deliveries():
    """Try again for people whose message failed on the way out.

    Bounded three ways, because an unbounded retry against a disconnected
    number bills forever: only FAILED rows, only a few attempts each, and only
    recent ones - a message from last week is no longer worth delivering even
    if it now could be.

    SKIPPED rows are never retried. A skip is a decision - muted, opted out,
    over budget - not a failure, and re-deciding it every hour would eventually
    send somebody a message they asked not to receive.
    """
    from sms.outbound import retry_delivery

    cutoff = timezone.now() - timedelta(hours=settings.SMS_RETRY_WINDOW_HOURS)
    stale = (
        SmsDelivery.objects.filter(
            direction=SmsDelivery.Direction.OUTBOUND,
            status=SmsDelivery.Status.FAILED,
            attempts__lt=settings.SMS_MAX_DELIVERY_ATTEMPTS,
            created_at__gte=cutoff,
        )
        .exclude(reason__in=PERMANENT_REASONS)
        # `message__sender_participant` is not decoration: retry_delivery reads
        # the message to rebuild the body AND to ask whether the recipient has
        # blocked its sender since. Without it that is two lazy queries per row,
        # up to four hundred a sweep.
        .select_related(
            'conversation',
            'participant__user__profile',
            'message__sender_participant',
        )[:200]
    )

    retried = 0
    for delivery in stale:
        if retry_delivery(delivery):
            retried += 1
    if retried:
        logger.info('Retried %s failed SMS deliveries', retried)
    return retried


def requeue_stalled_deliveries():
    """Rescue rows left in SENDING by a process that died mid-send.

    A row is marked SENDING before the provider call and moved on after it, so
    anything still SENDING long afterwards belongs to a worker that is gone.
    Left alone it stays there forever: it is not FAILED, so nothing retries it,
    and it is not SENT, so nobody knows the message did not arrive.
    """
    cutoff = timezone.now() - timedelta(minutes=settings.SMS_STALLED_AFTER_MINUTES)
    stalled = SmsDelivery.objects.filter(
        status=SmsDelivery.Status.SENDING, updated_at__lt=cutoff
    )
    count = stalled.update(
        status=SmsDelivery.Status.FAILED,
        reason='no answer from the sender process',
    )
    if count:
        # WARNING rather than INFO: this means something died, and finding out
        # from a delivery report is late.
        logger.warning('Marked %s stalled SMS deliveries as failed', count)
    return count


def provision_missing_numbers():
    """Give a number to any group that never got one.

    A group created while the provider was unreachable is app-only and stays
    that way, because nothing tries again. Its members can talk in the app and
    cannot be reached by text at all, which looks like the SMS feature being
    broken rather than one order having failed.

    Only groups with at least one SMS leg switched on are considered - delivery
    OR replies, matching `provisioning.uses_sms`. A group with both off either
    had them turned off by an admin, was created with both unticked (rule 1c),
    or never qualified for a number at all (rule 1a - its creator has no
    verified number). Buying one for any of those is spending money every month
    on a channel nobody asked for.

    Turning either setting back on is the signal to provision, and this job acts
    on it the next time it runs - so enabling SMS on an old group is a change of
    setting, not a support ticket.
    """
    from sms.provisioning import provision_for

    groups = (
        Conversation.objects.filter(
            Q(sms_delivery_enabled=True) | Q(sms_replies_enabled=True),
            kind=Conversation.Kind.GROUP,
            sms_number=None,
        )
        .select_related('created_by__profile')
        .order_by('created_at')[:settings.SMS_PROVISION_BATCH]
    )

    provisioned = 0
    for group in groups:
        try:
            if provision_for(group, group.created_by) is not None:
                provisioned += 1
        except Exception:
            # One group that cannot be provisioned must not stop the others.
            logger.exception('Could not provision a number for conversation %s', group.pk)
    if provisioned:
        logger.info('Provisioned numbers for %s groups that had none', provisioned)
    return provisioned


def announce_failed_deliveries():
    """Tell senders when text has given up on their message.

    The mirror of `retry_failed_deliveries` above, and deliberately written
    next to it: everything that sweep will NOT pick up again - a permanent
    reason, the attempts spent, or the retry window closed.

    Bounded to the last day. An alert about a message from last week is
    archaeology: it has been read, forgotten, or asked about in person.

    One alert per MESSAGE, not per person - see notification/delivery_alerts.py.
    """
    from notification.delivery_alerts import announce_all
    from notification.models import Notification

    now = timezone.now()
    retry_cutoff = now - timedelta(hours=settings.SMS_RETRY_WINDOW_HOURS)

    spent = Q(status=SmsDelivery.Status.FAILED) & (
        Q(reason__in=PERMANENT_REASONS)
        | Q(attempts__gte=settings.SMS_MAX_DELIVERY_ATTEMPTS)
        | Q(created_at__lt=retry_cutoff)
    )

    counts = (
        SmsDelivery.objects.filter(
            direction=SmsDelivery.Direction.OUTBOUND,
            created_at__gte=now - timedelta(days=1),
            message__isnull=False,
        )
        .filter(spent)
        .values('message_id')
        .annotate(failed=Count('id'))
        # Ordering comes from Meta and would otherwise join its way into the
        # GROUP BY, which makes the count per row rather than per message.
        .order_by()[:200]
    )

    wanted = [row['message_id'] for row in counts]
    if not wanted:
        return 0

    messages = {
        message.pk: message
        for message in Message.objects.filter(pk__in=wanted).select_related(
            'conversation', 'sender'
        )
    }

    announced = announce_all(
        (
            (messages[row['message_id']], row['failed'])
            for row in counts
            if row['message_id'] in messages
        ),
        Notification.Type.SMS_SEND_FAILED,
    )
    if announced:
        logger.info('Announced text delivery failures on %s messages', announced)
    return announced


def purge_old_deliveries():
    """Drop delivery and webhook records past their retention.

    This table gains a row per recipient per message and never loses one, so
    without this it is the fastest-growing thing in the database - and it holds
    a record of who texts which private group, which is not something to keep
    forever by accident.

    What billing needs from it outlives it in SmsUsageMonth, which
    `summarize_usage` keeps current and this never touches.
    """
    cutoff = timezone.now() - timedelta(days=settings.SMS_RETENTION_DAYS)
    deliveries, _ = SmsDelivery.objects.filter(created_at__lt=cutoff).delete()
    events, _ = SmsWebhookEvent.objects.filter(received_at__lt=cutoff).delete()
    # The code-text log keeps a number's hash against an account, so it goes
    # on the same clock.
    codes, _ = VerificationText.objects.filter(created_at__lt=cutoff).delete()
    if deliveries or events or codes:
        logger.info(
            'Purged %s deliveries, %s webhook events and %s code texts',
            deliveries, events, codes,
        )
    return deliveries, events


def summarize_usage(today=None):
    """Write this month's and last month's totals into SmsUsageMonth.

    Recomputed from the detail each time rather than added to, so a status
    update or a retry that lands days late is counted right, and running this
    twice changes nothing. Last month is redone too because its final days are
    still settling on the first of the next.

    A month is only recomputed while ALL of its detail is still kept: once the
    purge has started on it, the totals written while it was whole stand. With
    the default 90-day retention that never comes up - last month always
    started at most 62 days ago - but a shorter SMS_RETENTION_DAYS would
    otherwise overwrite a finished month with a smaller one.

    Months are UTC, the same clock every timestamp here is stored in.
    """
    now = timezone.now()
    if today is not None:
        # A given day is run as if it were that day: the retention line moves
        # with it, which is what lets a past month be summarised the way the
        # daily job did while its detail was whole.
        now = datetime(today.year, today.month, today.day, tzinfo=dt_timezone.utc)
    today = now.astimezone(dt_timezone.utc).date()
    this_month = today.replace(day=1)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    oldest_kept = now - timedelta(days=settings.SMS_RETENTION_DAYS)

    written = 0
    for month in (last_month, this_month):
        start = datetime(month.year, month.month, 1, tzinfo=dt_timezone.utc)
        # Rentals first, and whatever the retention: they are rebuilt from
        # NumberLease, which is never purged.
        written += _summarize_rentals(month, start, _next_month(month))
        if start < oldest_kept:
            continue
        written += _summarize_month(month, start, _next_month(month))
    return written


def _summarize_rentals(month, start, end):
    """A rental row for every number held at any point in this month."""
    leases = NumberLease.objects.filter(leased_at__lt=end).filter(
        Q(released_at__isnull=True) | Q(released_at__gte=start)
    )
    count = 0
    for lease in leases:
        NumberRentalMonth.objects.update_or_create(
            month=month,
            lease=lease,
            defaults={
                'number': lease.number,
                'conversation_id': lease.conversation_id,
                'conversation_name': lease.conversation_name,
                'monthly_cost': lease.monthly_cost,
                # Charged once, in the month it was bought.
                'upfront_cost': (
                    lease.upfront_cost if start <= lease.leased_at < end else None
                ),
                'cost_currency': lease.cost_currency,
            },
        )
        count += 1
    return count


def _code_text_lines(since):
    """The sign-up and login code texts of the last day: cost, fate, and shape.

    The shape is what shows abuse. A run of requests stopped by our limits, or
    codes going to far more different numbers than there are new sign-ups, is
    somebody using the sign-up form to send paid texts - each one billed to us
    whether it arrives or not.
    """
    rows = VerificationText.objects.filter(created_at__gte=since)
    Outcome = VerificationText.Outcome
    totals = rows.aggregate(
        requested=Count('pk'),
        sent=Count('pk', filter=Q(outcome=Outcome.SENT)),
        limited=Count('pk', filter=Q(outcome=Outcome.LIMITED)),
        failed=Count('pk', filter=Q(outcome=Outcome.FAILED)),
        country=Count('pk', filter=Q(outcome=Outcome.BLOCKED_COUNTRY)),
        delivered=Count('pk', filter=Q(delivery_status='delivered')),
        undelivered=Count('pk', filter=Q(delivery_status='failed')),
        numbers=Count('counterpart_hash', distinct=True),
        total_cost=Sum('cost'),
    )
    if not totals['requested']:
        return []

    currency = (
        rows.exclude(cost_currency='').values_list('cost_currency', flat=True).first()
        or ''
    )
    countries = (
        rows.exclude(country='').values('country').annotate(n=Count('pk'))
        .order_by('-n')[:5]
    )
    lines = [
        '',
        '  sign-up and login codes',
        f'    requested         {totals["requested"]}',
        f'    sent              {totals["sent"]}',
        f'    stopped by limits {totals["limited"]}',
        f'    refused by Telnyx {totals["failed"]}',
        f'    country refused   {totals["country"]}',
        f'    delivered         {totals["delivered"]}',
        f'    not delivered     {totals["undelivered"]}',
        f'    different numbers {totals["numbers"]}',
        '    cost              '
        + (
            f'{totals["total_cost"]:.4f} {currency}'
            if totals['total_cost'] is not None
            else 'not reported yet'
        ),
    ]
    if countries:
        lines.append(
            '    by country        '
            + ', '.join(f'{c["country"]} {c["n"]}' for c in countries)
        )
    failures = (
        rows.exclude(error_code='').values('error_code', 'error_detail')
        .annotate(n=Count('pk')).order_by('-n')[:5]
    )
    if failures:
        lines.append('    not delivered, by carrier error:')
        lines += [
            f'      {f["n"]:>5}  {f["error_code"]} {f["error_detail"]}'.rstrip()
            for f in failures
        ]

    alerts = []
    if totals['limited'] >= settings.VERIFICATION_ALERT_LIMITED:
        alerts.append(
            f'  *** {totals["limited"]} code requests were stopped by limits - '
            'possible abuse ***'
        )
    if totals['numbers'] >= settings.VERIFICATION_ALERT_NUMBERS:
        alerts.append(
            f'  *** codes went to {totals["numbers"]} different numbers - '
            'check for abuse ***'
        )
    if alerts:
        lines += [''] + alerts
    return lines


def _rental():
    """What the numbers held right now cost a month, from their rental record."""
    held = NumberLease.objects.filter(released_at__isnull=True)
    priced = held.exclude(monthly_cost=None)
    total = priced.aggregate(total=Sum('monthly_cost'))['total']
    unknown = held.count() - priced.count()
    currency = priced.values_list('cost_currency', flat=True).first() or ''
    note = f'  ({unknown} with no known price)' if unknown else ''
    if total is None:
        return f'price not known{note}'
    return f'{total:.2f} {currency} a month{note}'


def _money(rows, totals):
    """The day's cost as one line, saying when part of it is not priced yet."""
    if not totals['priced']:
        return 'not reported yet'
    currency = (
        rows.exclude(cost_currency='').values_list('cost_currency', flat=True).first()
        or ''
    )
    sent = rows.exclude(status=SmsDelivery.Status.SKIPPED).count()
    note = '' if totals['priced'] >= sent else f'  ({sent - totals["priced"]} not priced yet)'
    # Four places, as the provider prices a text; the database may hand the
    # sum back with as many trailing zeros as it likes.
    return f'{totals["total_cost"]:.4f} {currency}{note}'


def _next_month(month):
    if month.month == 12:
        return datetime(month.year + 1, 1, 1, tzinfo=dt_timezone.utc)
    return datetime(month.year, month.month + 1, 1, tzinfo=dt_timezone.utc)


def _summarize_month(month, start, end):
    rows = (
        SmsDelivery.objects.filter(created_at__gte=start, created_at__lt=end)
        .values('conversation', 'conversation__name', 'user', 'direction')
        .annotate(
            texts=Count('pk'),
            total_segments=Sum('segments'),
            delivered=Count('pk', filter=Q(status=SmsDelivery.Status.DELIVERED)),
            failed=Count('pk', filter=Q(status=SmsDelivery.Status.FAILED)),
            skipped=Count('pk', filter=Q(status=SmsDelivery.Status.SKIPPED)),
            total_cost=Sum('cost'),
            priced=Count('cost'),
            # Smallest and largest currency code seen: equal unless they mix.
            first_currency=Min('cost_currency', filter=~Q(cost_currency='')),
            last_currency=Max('cost_currency', filter=~Q(cost_currency='')),
        )
        .order_by()
    )

    for row in rows:
        if row['first_currency'] != row['last_currency']:
            # One provider account bills in one currency, so this should not
            # happen; if it does, the sum below is meaningless and says so.
            logger.error(
                'SMS usage for %s in %s mixes currencies %s and %s; its cost is '
                'not a single amount', row['conversation'], month,
                row['first_currency'], row['last_currency'],
            )
        SmsUsageMonth.objects.update_or_create(
            month=month,
            conversation_id=row['conversation'],
            user_id=row['user'],
            direction=row['direction'],
            defaults={
                'conversation_name': (row['conversation__name'] or '')[:120],
                'texts': row['texts'],
                'segments': row['total_segments'] or 0,
                'delivered': row['delivered'],
                'failed': row['failed'],
                'skipped': row['skipped'],
                'cost': row['total_cost'] or 0,
                'cost_currency': row['first_currency'] or '',
                'priced': row['priced'],
            },
        )
    return len(rows)


# The carrier codes seen on code texts so far, in Telnyx's words. Its message
# records carry the code alone; anything not here is shown as the bare code,
# which is still something to look up rather than a guessed explanation.
CARRIER_ERRORS = {
    '40008': 'Undeliverable',
    '40010': 'Not 10DLC registered',
    '40012': 'Invalid messaging destination number',
}

# How far back each run looks, and how close in time a Telnyx record has to be
# to our own request to be the same text. Codes to one number are capped at a
# few an hour (OTP_MAX_SENDS_PER_HOUR), so ten minutes cannot pair the wrong two.
RECONCILE_LOOKBACK = timedelta(hours=48)
RECONCILE_MATCH_WINDOW = timedelta(minutes=10)


def reconcile_verification_texts():
    """Fill in what each code text cost and whether it arrived, from Telnyx.

    Its message records are the only place it says; the Verify API and its
    webhooks carry neither. Each record is paired with our own row for the same
    number sent within RECONCILE_MATCH_WINDOW - by the number's hash, which is
    all we keep - and a record already paired is refreshed rather than paired
    again, so a status that settles later is picked up on the next run.

    Group texts are skipped by their messaging profile. Everything else is
    left to the pairing: a record with no row of ours to match is simply not
    ours to record.
    """
    from sms.client import SmsError, message_records_since

    if not settings.TELNYX_API_KEY:
        return 0

    since = timezone.now() - RECONCILE_LOOKBACK
    rows = list(
        VerificationText.objects.filter(
            outcome=VerificationText.Outcome.SENT,
            created_at__gte=since - RECONCILE_MATCH_WINDOW,
        )
    )
    if not rows:
        return 0

    try:
        records = message_records_since(since.astimezone(dt_timezone.utc))
    except SmsError:
        logger.exception('Could not read Telnyx message records for code texts')
        return 0

    paired = {row.provider_record_id: row for row in rows if row.provider_record_id}
    waiting = defaultdict(list)
    for row in rows:
        if not row.provider_record_id:
            waiting[row.counterpart_hash].append(row)

    updated = 0
    for record in records:
        if record.get('direction') != 'outbound':
            continue
        if record.get('profile_id') and (
            record['profile_id'] == settings.TELNYX_MESSAGING_PROFILE_ID
        ):
            continue
        record_id = str(record.get('id') or '')
        if not record_id:
            continue

        row = paired.get(record_id)
        if row is None:
            row = _pair(record, waiting)
            if row is None:
                continue
            paired[record_id] = row
        _fill_from_record(row, record_id, record)
        updated += 1
    return updated


def _pair(record, waiting):
    """Our unpaired row for this record's number, nearest in time, or None."""
    number = (record.get('cld') or '').strip()
    sent_at = parse_datetime(record.get('created_at') or '')
    if not number or sent_at is None:
        return None
    candidates = [
        row for row in waiting.get(hashlib.sha256(number.encode()).hexdigest(), [])
        if abs(row.created_at - sent_at) <= RECONCILE_MATCH_WINDOW
    ]
    if not candidates:
        return None
    row = min(candidates, key=lambda r: abs(r.created_at - sent_at))
    waiting[row.counterpart_hash].remove(row)
    return row


def _fill_from_record(row, record_id, record):
    status = record.get('status') or ''
    row.provider_record_id = record_id
    # Telnyx's final words are delivered and failed; anything else (a receipt
    # that never came, a text still on its way) is not yet a fact either way.
    row.delivery_status = status if status in ('delivered', 'failed') else 'unconfirmed'
    try:
        cost = record.get('cost')
        row.cost = Decimal(str(cost)) if cost not in (None, '') else None
    except (InvalidOperation, TypeError, ValueError):
        row.cost = None
    row.cost_currency = str(record.get('currency') or '')[:3].upper()

    errors = record.get('errors') or []
    first = errors[0] if errors else ''
    code = str(first.get('code') if isinstance(first, dict) else first or '')[:16]
    row.error_code = code
    row.error_detail = CARRIER_ERRORS.get(code, '')
    row.save(update_fields=[
        'provider_record_id', 'delivery_status', 'cost', 'cost_currency',
        'error_code', 'error_detail',
    ])


def daily_spend_report():
    """Email yesterday's SMS spend, and say if anything hit a ceiling.

    The cheapest possible cost alarm: a budget that stops sending does so
    silently, and the first sign otherwise is somebody asking why their group
    went quiet. One mail a day makes both the spend and the silence visible.
    """
    from utils.mail import EmailError, send_email

    since = timezone.now() - timedelta(days=1)
    rows = SmsDelivery.objects.filter(created_at__gte=since)

    totals = rows.aggregate(
        total_cost=Sum('cost'),
        priced=Count('cost'),
        segments=Sum('segments'),
        sent=Count('pk', filter=Q(status=SmsDelivery.Status.SENT)),
        delivered=Count('pk', filter=Q(status=SmsDelivery.Status.DELIVERED)),
        failed=Count('pk', filter=Q(status=SmsDelivery.Status.FAILED)),
        skipped=Count('pk', filter=Q(status=SmsDelivery.Status.SKIPPED)),
        inbound=Count('pk', filter=Q(direction=SmsDelivery.Direction.INBOUND)),
    )

    over_budget = rows.filter(reason__icontains='budget').count()
    switched_off = rows.filter(reason='sending is switched off').count()

    lines = [
        'LoopD SMS, last 24 hours',
        '',
        f'  billed segments   {totals["segments"] or 0}',
        f'  cost              {_money(rows, totals)}',
        f'  sent              {totals["sent"]}',
        f'  delivered         {totals["delivered"]}',
        f'  failed            {totals["failed"]}',
        f'  skipped           {totals["skipped"]}',
        f'  inbound           {totals["inbound"]}',
        '',
        f'  numbers leased    {GroupNumber.objects.count()}',
        f'  number rental     {_rental()}',
        f'  groups with none  '
        f'{Conversation.objects.filter(kind="group", sms_number=None).count()}',
    ]
    # Why things failed, in the provider's words. A single code dominating
    # this list - 40010, say - is a registration problem, not bad luck.
    failures = (
        rows.filter(status=SmsDelivery.Status.FAILED)
        .exclude(error_code='')
        .values('error_code', 'error_detail')
        .annotate(n=Count('pk'))
        .order_by('-n')[:5]
    )
    if failures:
        lines += ['', '  failed, by provider error:']
        lines += [
            f'    {f["n"]:>5}  {f["error_code"]} {f["error_detail"]}'
            for f in failures
        ]
    lines += _code_text_lines(since)
    if over_budget:
        lines += ['', f'  *** {over_budget} messages were NOT sent: over budget ***']
    if switched_off:
        lines += ['', f'  *** {switched_off} messages were NOT sent: sending is off ***']

    body = '\n'.join(lines)
    logger.info('Daily SMS report:\n%s', body)

    recipient = settings.SMS_REPORT_EMAIL
    if not recipient:
        # No address configured is a decision, not a failure - the report is in
        # the log either way.
        return body

    try:
        send_email(to=recipient, subject='LoopD SMS daily report', html=f'<pre>{body}</pre>',
                   text=body)
    except EmailError:
        logger.exception('Could not email the daily SMS report')
    return body
