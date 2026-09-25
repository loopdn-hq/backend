"""Copy the consent question onto the row that holds the answer.

`Participant.consent_sent_at` and `consent_text` recorded when somebody was
asked and in what words - but one pair of fields per MEMBER, when consent is
per member per CHANNEL. This moves the existing record onto ChannelConsent
beside the answer it belongs to.

Existing rows are compliance evidence. Nothing is deleted here and the old
columns stay put: they are dropped in a later migration, once no running code
reads them. Migrations run before the new image in this deploy path, so a drop
in the same release would break the code still serving traffic.
"""
from django.db import migrations


def copy_question_onto_consent(apps, schema_editor):
    Participant = apps.get_model('chat', 'Participant')
    ChannelConsent = apps.get_model('chat', 'ChannelConsent')

    # Only members who were actually asked something. A row with neither a date
    # nor wording has no question to move.
    asked = Participant.objects.exclude(
        consent_sent_at__isnull=True, consent_text=''
    ).only('id', 'consent_sent_at', 'consent_text')

    for participant in asked.iterator():
        rows = ChannelConsent.objects.filter(participant_id=participant.id)

        # Attributed to EVERY channel this member has a consent row for, which
        # is the honest reading: the old field could not say which channel the
        # question was asked on, so claiming one would be inventing precision
        # the data never had.
        for consent in rows:
            changed = []
            if consent.asked_at is None and participant.consent_sent_at:
                consent.asked_at = participant.consent_sent_at
                changed.append('asked_at')
            if not consent.consent_text and participant.consent_text:
                consent.consent_text = participant.consent_text
                changed.append('consent_text')
            if changed:
                consent.save(update_fields=changed)


def uncopy(apps, schema_editor):
    """Reversible, and deliberately a no-op.

    The Participant columns still hold everything this migration copied, so
    there is nothing to restore. Blanking the new columns would only destroy a
    record that a later forward run would have to reconstruct.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0027_channel_consent_remembers_the_question'),
    ]

    operations = [
        migrations.RunPython(copy_question_onto_consent, uncopy),
    ]
