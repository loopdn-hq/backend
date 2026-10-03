"""Slice 6: which press of a card's button counts as its click.

Nothing has written an AdClick before slice 6, so every existing row (none)
is uncounted.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0010_serving_and_measurement'),
    ]

    operations = [
        migrations.AddField(
            model_name='adclick',
            name='counted',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddConstraint(
            model_name='adclick',
            constraint=models.UniqueConstraint(
                condition=models.Q(counted=True),
                fields=('impression',),
                name='commercial_click_counted_once',
            ),
        ),
        migrations.AddIndex(
            model_name='adclick',
            index=models.Index(
                condition=models.Q(counted=True),
                fields=['clicked_at'],
                name='commercial_click_counted_idx',
            ),
        ),
    ]
