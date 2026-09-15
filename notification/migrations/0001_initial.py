"""Notification, taken over from the chat app.

STATE ONLY - this runs no SQL. The table `chat_notification` was created by
chat/0011 and already holds every row; chat/0022 stopped chat tracking the
model, and this starts tracking it here, pointed at that same table and index.
The model's Meta pins both names, so nothing is ever renamed or copied.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        # Must run after chat lets go of the model: both states at once would
        # give User and Conversation two `notifications` reverse accessors.
        ('chat', '0022_move_notification_to_notification_app'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='Notification',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('type', models.CharField(choices=[('member.added', 'Added to a group'), ('member.removed', 'Removed from a group'), ('role.granted', 'Made an admin'), ('role.revoked', 'No longer an admin'), ('join.requested', 'Someone asked to join'), ('join.approved', 'Your request was approved'), ('join.declined', 'Your request was declined'), ('group.mode_changed', 'Switched to broadcast'), ('sms.opted_out', 'A member texted STOP'), ('sms.opted_in', 'A member accepted by text'), ('sms.send_failed', 'A message did not reach everyone'), ('sms.invite_pending', 'An invitation is unanswered'), ('general', 'General')], default='general', max_length=60)),
                        ('data', models.JSONField(blank=True, null=True)),
                        ('read_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('conversation', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to='chat.conversation')),
                        ('from_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sent_notifications', to=settings.AUTH_USER_MODEL)),
                        ('message', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='chat.message')),
                        ('to_user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'chat_notification',
                        'ordering': ['-created_at'],
                        'indexes': [models.Index(fields=['to_user', '-created_at'], name='chat_notifi_to_user_be9887_idx')],
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
