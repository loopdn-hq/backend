from django.db import migrations, models


class Migration(migrations.Migration):
    """Written by hand, so it carries only this field.

    `makemigrations chat` would also sweep in an unrelated pending Meta change
    on MessageReaction that is not part of this work.
    """

    dependencies = [
        ('chat', '0020_conversation_pinned_message_message_deleted_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='link_preview',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
