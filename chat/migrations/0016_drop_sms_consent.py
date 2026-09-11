"""Stop Django reading Participant.sms_consent. Leave the column where it is.

A state-only removal, and the difference matters more than it looks.

THE DEPLOY ORDER IS WHY
-----------------------
.github/workflows/deploy-to-aws-staging.yml runs `migrate` as a one-off ECS
task and only THEN calls `update-service`. So for the length of a rollout the
new schema is live and the PREVIOUS image is still answering requests - and
that image's Participant model declares `sms_consent` as a concrete field,
which Django names in the column list of every SELECT that materialises a
Participant.

A real `ALTER TABLE ... DROP COLUMN` here would therefore take the whole chat
surface down for the rollout window: create_message, the conversation list, the
SMS fan-out. Worse, it would be one-way - rolling the service back to the
previous task definition puts that same code against the new schema, so the
only exit is reversing the migration, which is its own data hazard.

So the field leaves Django's state and the column stays in Postgres. New code
never selects it and never writes it; old code keeps working because the column
is still there, still NOT NULL, and still carries `db_default='not_required'`,
which is what makes an INSERT from the new code valid.

THE ACTUAL DROP IS A LATER RELEASE
----------------------------------
Once this has been deployed and every task is running code that does not know
the field, a follow-up migration can issue

    ALTER TABLE chat_participant DROP COLUMN sms_consent;

as RunSQL. Deliberately not written yet: shipping it in this release would put
the drop back in the same `migrate` run and undo the whole point.

Until then the column is dead weight of a few bytes a row, which is the price
of a rollout nobody notices - and it doubles as the verification window
0015 asks for, where ChannelConsent and the old column can be compared against
each other before anything is destroyed.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0015_backfill_sms_consent'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Nothing happens to the database. That is the entire point.
            database_operations=[],
            state_operations=[
                migrations.RemoveField(
                    model_name='participant',
                    name='sms_consent',
                ),
            ],
        ),
    ]
