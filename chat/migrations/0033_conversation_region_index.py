"""An index for matching campaigns on a group's region.

A campaign names regions outright ("PH-00") or means every region of a country
by prefix ("PH-%"); varchar_pattern_ops serves both. Category needs none: its
foreign key is indexed already (0032).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0032_group_category_and_region'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='conversation',
            index=models.Index(
                fields=['region'],
                name='chat_conversation_region_idx',
                opclasses=['varchar_pattern_ops'],
            ),
        ),
    ]
