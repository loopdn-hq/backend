"""Move the account-wide SMS and email switches from Profile to MessagingSettings.

`sms_enabled`, `sms_opt_out_at`, `sms_consent_at` and `email_enabled` are
consent: a carrier STOP/START, a bounce and a complaint write them, and the
delivery code reads them before every text and email. This migration:

1. adds them to MessagingSettings;
2. copies every account's values across, creating the MessagingSettings row
   for any account that has none - every account must have one, because the
   delivery code asks it about every member of a group;
3. removes them from Profile in Django's STATE ONLY.

WHY THE COLUMNS STAY - the same reason as chat/0016_drop_sms_consent
--------------------------------------------------------------------
`migrate` runs as a one-off task before the service updates, so for the length
of a rollout the previous image is still serving, and its Profile model still
selects and writes these columns. Dropping them here would break every request
that loads a Profile during the rollout. They stay, NOT NULL with their
database defaults, which is also what keeps an INSERT from the new code valid.

THE FOLLOW-UP RELEASE MUST, IN THIS ORDER
-----------------------------------------
1. Re-apply any opt-out the OLD code recorded on Profile after step 2 ran - a
   STOP or a complaint handled by the previous image mid-rollout. Subtract
   only: where the Profile column says off and MessagingSettings says on, turn
   it off, with the timestamp. A STOP that under-silences is a compliance
   failure, and that rollout window is the one place it could slip.
2. Then drop the four columns with RunSQL - they are no longer in Django's
   state, so a RemoveField cannot do it:

       ALTER TABLE user_profile DROP COLUMN sms_enabled;
       ALTER TABLE user_profile DROP COLUMN sms_opt_out_at;
       ALTER TABLE user_profile DROP COLUMN sms_consent_at;
       ALTER TABLE user_profile DROP COLUMN email_enabled;

Deliberately not written yet: shipping it in this release would put the drop
back into the same `migrate` run and undo the point.
"""
from django.db import migrations, models

SWITCHES = ('sms_enabled', 'sms_opt_out_at', 'sms_consent_at', 'email_enabled')


def copy_to_messaging_settings(apps, schema_editor):
    Profile = apps.get_model('user', 'Profile')
    MessagingSettings = apps.get_model('user', 'MessagingSettings')

    existing = {row.user_id: row for row in MessagingSettings.objects.all()}
    to_create, to_update = [], []
    for values in Profile.objects.values('user_id', *SWITCHES).iterator():
        user_id = values.pop('user_id')
        row = existing.get(user_id)
        if row is None:
            to_create.append(MessagingSettings(user_id=user_id, **values))
        else:
            for name, value in values.items():
                setattr(row, name, value)
            to_update.append(row)

    MessagingSettings.objects.bulk_create(to_create, batch_size=500)
    MessagingSettings.objects.bulk_update(to_update, SWITCHES, batch_size=500)


def copy_back_to_profile(apps, schema_editor):
    """The way back, for a rollback: the columns never left, so refill them."""
    Profile = apps.get_model('user', 'Profile')
    MessagingSettings = apps.get_model('user', 'MessagingSettings')

    for values in MessagingSettings.objects.values('user_id', *SWITCHES).iterator():
        user_id = values.pop('user_id')
        Profile.objects.filter(user_id=user_id).update(**values)


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0008_settings_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='messagingsettings',
            name='sms_enabled',
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AddField(
            model_name='messagingsettings',
            name='sms_opt_out_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='messagingsettings',
            name='sms_consent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='messagingsettings',
            name='email_enabled',
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.RunPython(copy_to_messaging_settings, copy_back_to_profile),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='profile', name=name)
                for name in SWITCHES
            ],
            database_operations=[],
        ),
    ]
