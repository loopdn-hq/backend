"""Drop the four delivery switches Profile kept after user/0009.

The follow-up 0009's docstring asked for, in the order it asked:

1. RE-APPLY STRAY OPT-OUTS. For the length of the 0009 rollout the previous
   image was still serving, and it wrote a STOP or a complaint to Profile, not
   to MessagingSettings. Anything Profile says is OFF while MessagingSettings
   says ON is carried across - subtract only, never the other way.

   One exception, for SMS only: a START recorded on MessagingSettings AFTER
   the STOP on Profile means the person asked for texts back since, so they
   are left on. Email has no such record from that period, so it errs the way
   0009 said to: an opt-out that under-silences is a compliance failure, one
   that over-silences is an annoyance.

2. DROP THE COLUMNS, with RunSQL - they left Django's state in 0009, so a
   RemoveField cannot reach them. The reverse adds them back with their
   defaults, so rolling back past 0009 still works; 0009's own reverse then
   refills them.

DEPLOY ORDER - READ BEFORE SHIPPING
-----------------------------------
This must NOT reach an environment in the same `migrate` run as 0009. While a
deploy rolls out the previous image keeps serving, and an image from before
0009 still reads these columns every time it loads a Profile. Ship 0009, let
that deploy finish, then ship this.
"""
import datetime

from django.db import migrations
from django.utils import timezone
from django.utils.dateparse import parse_datetime

OLD_COLUMNS = ('sms_enabled', 'sms_opt_out_at', 'sms_consent_at', 'email_enabled')


def _aware(value):
    """A timestamp read with raw SQL, as the ORM would have returned it.

    SQLite hands back a naive value, or even a string; Postgres an aware one.
    """
    if isinstance(value, str):
        value = parse_datetime(value)
    if value is not None and timezone.is_naive(value):
        value = timezone.make_aware(value, datetime.timezone.utc)
    return value


def reapply_stray_opt_outs(apps, schema_editor):
    MessagingSettings = apps.get_model('user', 'MessagingSettings')

    # Raw SQL: these columns are no longer on the historical Profile model.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'SELECT user_id, sms_enabled, sms_opt_out_at, email_enabled '
            'FROM user_profile WHERE sms_enabled = %s OR email_enabled = %s',
            [False, False],
        )
        rows = cursor.fetchall()

    now = timezone.now()
    for user_id, sms_on, stopped_at, email_on in rows:
        stopped_at = _aware(stopped_at)
        # get_or_create: every account has a row, but an opt-out must stick
        # even for one that somehow does not.
        switches, _ = MessagingSettings.objects.get_or_create(user_id=user_id)
        changed = []

        started_since = switches.sms_consent_at is not None and (
            stopped_at is None or switches.sms_consent_at > stopped_at
        )
        if not sms_on and switches.sms_enabled and not started_since:
            switches.sms_enabled = False
            switches.sms_opt_out_at = stopped_at or now
            changed += ['sms_enabled', 'sms_opt_out_at']

        if not email_on and switches.email_enabled:
            switches.email_enabled = False
            switches.email_opt_out_at = now
            changed += ['email_enabled', 'email_opt_out_at']

        if changed:
            switches.save(update_fields=[*changed, 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0011_email_opt_out_record'),
    ]

    operations = [
        migrations.RunPython(reapply_stray_opt_outs, migrations.RunPython.noop),
        migrations.RunSQL(
            sql=[f'ALTER TABLE user_profile DROP COLUMN {name}' for name in OLD_COLUMNS],
            # As 0005 and 0006 created them: both switches on by default in the
            # database itself, both timestamps empty.
            reverse_sql=[
                'ALTER TABLE user_profile ADD COLUMN sms_enabled boolean DEFAULT true NOT NULL',
                'ALTER TABLE user_profile ADD COLUMN sms_opt_out_at timestamp with time zone NULL',
                'ALTER TABLE user_profile ADD COLUMN sms_consent_at timestamp with time zone NULL',
                'ALTER TABLE user_profile ADD COLUMN email_enabled boolean DEFAULT true NOT NULL',
            ],
        ),
    ]
