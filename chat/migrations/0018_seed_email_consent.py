"""Give every existing membership an answer on record for email.

`chat/signals.py` seeds consent when a membership is created, which covers
every membership made from here on and none of the ones that already exist. A
missing row reads as a refusal, so without this the email channel would ship
switched off for the entire user base and there would be nothing anywhere
saying why.

The default is the one the signal uses, for the reason given there: an account
holder is opted in, because they have an account and a verified address and
there was never a group question to ask; anybody else is pending, because
somebody put their number or address into a group and silence is not agreement.

No timestamps. `consented_at` says when a person answered, and nobody answered
this - it is a default, and recording a date would turn a default into evidence
of a decision that was never made.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Participant = apps.get_model('chat', 'Participant')
    ChannelConsent = apps.get_model('chat', 'ChannelConsent')

    already = set(
        ChannelConsent.objects.filter(channel='email').values_list(
            'participant_id', flat=True
        )
    )

    rows = [
        ChannelConsent(
            participant_id=participant.pk,
            channel='email',
            status='pending' if participant.user_id is None else 'opted_in',
        )
        for participant in Participant.objects.all().iterator()
        if participant.pk not in already
    ]

    ChannelConsent.objects.bulk_create(rows, batch_size=500)


def unseed(apps, schema_editor):
    ChannelConsent = apps.get_model('chat', 'ChannelConsent')
    ChannelConsent.objects.filter(channel='email').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0017_alter_message_source'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
