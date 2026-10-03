"""What kind of group this is and where: the two facts campaigns target on.

Both start empty on every group, and an admin declares them (the picker is
slice 4 of docs/campaigns_final_plan.md). Nullable and blank-defaulted, so
adding them rewrites nothing and a group that never says stays undeclared.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0031_seed_group_categories'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='category',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='conversations', to='chat.groupcategory'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='region',
            field=models.CharField(blank=True, db_default='', default='', max_length=6),
        ),
    ]
