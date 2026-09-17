from django.db import migrations


def release_accounts_awaiting_verification(apps, schema_editor):
    """Reactivate accounts that were held inactive purely for verification.

    There was a brief window in which registering created an INACTIVE account
    and `verify_email` flipped `is_active` back on. That is gone: is_active now
    means only "an admin has not banned this account", and signing in is gated
    on `Profile.email_verified` instead. See the note on that field.

    Accounts registered during that window are stranded - inactive, so the
    resend-code and verify-email lookups (which now require is_active=True)
    cannot find them, and there is no way for them to ever verify.

    Telling them apart from real bans is unambiguous: 0003 backfilled every
    profile that existed before this scheme to email_verified=True, so an
    account that is BOTH inactive AND unverified can only have come from that
    window. A genuine ban carries email_verified=True and is left alone.
    """
    Profile = apps.get_model('user', 'Profile')
    User = apps.get_model('auth', 'User')

    stranded = Profile.objects.filter(
        email_verified=False, user__is_active=False
    ).values_list('user_id', flat=True)

    User.objects.filter(pk__in=list(stranded)).update(is_active=True)


def noop(apps, schema_editor):
    """Not reversible: which accounts were bans is no longer recoverable.

    Deactivating them again would ban people who were never banned, which is
    worse than leaving them alone.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0003_profile_email_verified'),
    ]

    operations = [
        migrations.RunPython(release_accounts_awaiting_verification, noop),
    ]
