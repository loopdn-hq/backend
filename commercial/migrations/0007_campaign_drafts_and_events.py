"""Slice 1's schema: drafts that may be incomplete, placement, and history.

A draft saves as the builder goes, so its name and total budget may be empty
until submit (docs/campaigns_final_plan.md, section 7). The budget constraint
becomes "unset or positive". `placement` arrives with its only value, `chat`,
which every existing campaign takes. CampaignEvent is the history each
transition writes.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commercial', '0006_company_details'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CampaignEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('created', 'Created'), ('submitted', 'Submitted for review'), ('withdrawn', 'Withdrawn from review'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('funds_reserved', 'Budget reserved'), ('funds_released', 'Budget released'), ('started', 'Started'), ('paused', 'Paused'), ('resumed', 'Resumed'), ('budget_increased', 'Budget increased'), ('cancelled', 'Cancelled'), ('ended', 'Ended')], max_length=20)),
                ('actor_label', models.CharField(blank=True, db_default='', default='', max_length=150)),
                ('note', models.TextField(blank=True, db_default='', default='')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['created_at', 'pk'],
            },
        ),
        migrations.RemoveConstraint(
            model_name='campaign',
            name='commercial_campaign_has_a_budget',
        ),
        migrations.AddField(
            model_name='campaign',
            name='placement',
            field=models.CharField(choices=[('chat', 'In group chats')], db_default='chat', default='chat', max_length=16),
        ),
        migrations.AlterField(
            model_name='campaign',
            name='name',
            field=models.CharField(blank=True, db_default='', default='', max_length=120),
        ),
        migrations.AlterField(
            model_name='campaign',
            name='total_budget_cents',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='campaign',
            constraint=models.CheckConstraint(condition=models.Q(('total_budget_cents__isnull', True), ('total_budget_cents__gt', 0), _connector='OR'), name='commercial_campaign_budget_positive'),
        ),
        migrations.AddField(
            model_name='campaignevent',
            name='actor',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='campaignevent',
            name='campaign',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='commercial.campaign'),
        ),
        migrations.AddIndex(
            model_name='campaignevent',
            index=models.Index(fields=['campaign', 'created_at'], name='commercial_event_campaign_idx'),
        ),
    ]
