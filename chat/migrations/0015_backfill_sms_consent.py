"""Carry every existing SMS consent answer into ChannelConsent.

Without this the new table starts empty, every member reads as PENDING, and
the fan-out stops texting everybody at once - including people who agreed
months ago. The old column is not dropped here: this migration is reversible
and the two can be verified against each other before anything is deleted.
"""
from django.db import migrations


# Participant.Consent -> ChannelConsent.Status.
#
# NOT_REQUIRED is an account holder who agreed in their own settings, so there
# was never a group question to answer - they are opted in. GRANTED is somebody
# who answered it explicitly. Both mean "send".
STATUS_BY_CONSENT = {
    'not_required': 'opted_in',
    'granted': 'opted_in',
    'pending': 'pending',
    'stopped': 'opted_out',
}


def backfill(apps, schema_editor):
    Participant = apps.get_model('chat', 'Participant')
    ChannelConsent = apps.get_model('chat', 'ChannelConsent')

    rows = []
    for participant in Participant.objects.all().iterator():
        status = STATUS_BY_CONSENT.get(participant.sms_consent, 'pending')
        rows.append(
            ChannelConsent(
                participant_id=participant.pk,
                channel='sms',
                status=status,
                # The timestamps the old row already carried, so the record of
                # WHEN somebody agreed survives the move. consent_at is null
                # for anybody who never answered, which is correct.
                consented_at=participant.consent_at,
                opted_out_at=(
                    participant.consent_at if status == 'opted_out' else None
                ),
            )
        )

    ChannelConsent.objects.bulk_create(rows, batch_size=500)


def unbackfill(apps, schema_editor):
    ChannelConsent = apps.get_model('chat', 'ChannelConsent')
    ChannelConsent.objects.filter(channel='sms').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0014_channel_consent'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
