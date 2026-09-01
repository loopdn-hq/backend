from django.db import migrations, models


def mark_existing_accounts_verified(apps, schema_editor):
    """Every account that already exists predates email verification.

    The field defaults to False, which for a new registration is the truth. For
    a row that is already in the table it is not: those accounts were created
    when REQUIRE_EMAIL_VERIFICATION was off, so nobody was ever asked for a
    code, and leaving them False would mean resend-code and verify-email accept
    them - including any account an admin has deliberately deactivated, which
    is exactly the reactivation this flag exists to prevent.

    Backfilling True fails closed: no pre-existing account can be issued a
    verification code. Anyone who genuinely needs one is a new registration.
    """
    Profile = apps.get_model('user', 'Profile')
    Profile.objects.update(email_verified=True)


def unmark(apps, schema_editor):
    """Reversing only has to leave the column in a state AddField can drop."""
    Profile = apps.get_model('user', 'Profile')
    Profile.objects.update(email_verified=False)


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0002_profile_phone_no_verified'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='email_verified',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(mark_existing_accounts_verified, unmark),
    ]
