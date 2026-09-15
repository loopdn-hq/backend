"""Notification now belongs to the notification app.

STATE ONLY - this runs no SQL. Django stops tracking the model as chat's; the
table `chat_notification`, and every row in it, stays exactly where it is.
notification/0001 picks the same table up.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0021_message_link_preview'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name='Notification'),
            ],
            database_operations=[],
        ),
    ]
