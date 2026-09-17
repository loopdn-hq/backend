"""`sms_enabled` becomes the person's own switch, and STOP becomes per Loop.

Before this, `sms_enabled` was written only by a carrier STOP or START, and a
STOP to any Loop's number turned it off for the whole account. The person's own
choice in the app was a second field, `sms_to_loopd_users_enabled`.

Now `sms_enabled` IS the person's choice - "Send and receive Loop messages by
text" - and a STOP opts that number out of the one Loop it was sent to, which
sms/inbound.py has always also recorded on that Loop's ChannelConsent. So:

1. Every account's `sms_enabled` becomes what they chose in the app
   (`sms_to_loopd_users_enabled`), stamping `sms_opt_out_at` where that turns
   it off. An account switched off only by a STOP comes back on here; the
   STOP itself still holds, on the Loop it was sent to.
2. `sms_to_loopd_users_enabled` and `sms_consent_at` are removed.

The reverse re-adds both fields and refills `sms_to_loopd_users_enabled` from
`sms_enabled`. It cannot bring back an account-wide STOP, because none exists
any more.
"""
from django.db import migrations, models
from django.utils import timezone


def adopt_the_persons_choice(apps, schema_editor):
    MessagingSettings = apps.get_model('user', 'MessagingSettings')
    now = timezone.now()
    MessagingSettings.objects.filter(
        sms_to_loopd_users_enabled=False, sms_enabled=True
    ).update(sms_enabled=False, sms_opt_out_at=now, updated_at=now)
    MessagingSettings.objects.filter(
        sms_to_loopd_users_enabled=True, sms_enabled=False
    ).update(sms_enabled=True, updated_at=now)


def restore_the_app_choice(apps, schema_editor):
    MessagingSettings = apps.get_model('user', 'MessagingSettings')
    MessagingSettings.objects.update(sms_to_loopd_users_enabled=models.F('sms_enabled'))


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0012_drop_old_profile_delivery_columns'),
    ]

    operations = [
        migrations.RunPython(adopt_the_persons_choice, restore_the_app_choice),
        migrations.RemoveField(
            model_name='messagingsettings',
            name='sms_to_loopd_users_enabled',
        ),
        migrations.RemoveField(
            model_name='messagingsettings',
            name='sms_consent_at',
        ),
        migrations.AlterField(
            model_name='messagingsettings',
            name='sms_enabled',
            field=models.BooleanField(
                db_default=True,
                default=True,
                verbose_name='Send and receive Loop messages by text',
            ),
        ),
        migrations.AlterField(
            model_name='messagingsettings',
            name='sms_opt_out_at',
            field=models.DateTimeField(
                blank=True, null=True, verbose_name='SMS turned off at'
            ),
        ),
    ]
