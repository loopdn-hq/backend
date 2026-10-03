"""Make sure an environment has an admin: the account the admin site opens for.

    python manage.py create_admin --email admin@loopdn.com

Creates the account if there is none with that email, or brings an existing
one up to date: email verified, and the `admin` role granted (audited, like
any grant). Run it once per environment - a fresh database has no admin, and
nobody can grant the first one from the permissions screen.

THE PASSWORD IS NEVER IN CODE
It comes from the ADMIN_PASSWORD environment variable, or, for a new account
(or with --reset-password), is generated here and printed once. An existing
account's password is left alone unless --reset-password says otherwise, so
running this again is safe.

It will not switch a deactivated account back on: `is_active = False` is how
an account is banned, and a setup command must not undo that quietly.
"""
import os
import secrets
import string

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from rbac import services
from rbac.models import UserRole

PASSWORD_LENGTH = 16
_SYMBOLS = '!@#$%^&*-_=+?'


def generate_password(length=PASSWORD_LENGTH):
    """Random, from `secrets`, with a lower, an upper, a digit and a symbol."""
    alphabet = string.ascii_letters + string.digits + _SYMBOLS
    while True:
        password = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in _SYMBOLS for c in password)
        ):
            return password


class Command(BaseCommand):
    help = 'Create or update the admin account for this environment.'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument(
            '--username',
            help='For a new account; defaults to the part of the email before the @.',
        )
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help="Set a new password on an existing account (from ADMIN_PASSWORD, "
                 'or generated).',
        )

    def handle(self, *args, email, username=None, reset_password=False, **options):
        User = get_user_model()
        email = email.strip().lower()
        password = os.environ.get('ADMIN_PASSWORD') or None

        with transaction.atomic():
            user = User.objects.filter(email__iexact=email).first()
            created = user is None
            if created:
                username = (username or email.split('@')[0]).strip()
                if User.objects.filter(username__iexact=username).exists():
                    raise CommandError(
                        f'The username "{username}" is taken by another account. '
                        'Pass --username.'
                    )
                user = User(username=username, email=email, is_active=True)
            elif not user.is_active:
                raise CommandError(
                    f'{email} is deactivated. Reactivate it on purpose before '
                    'making it an admin.'
                )

            setting_password = created or reset_password
            generated = False
            if setting_password:
                if password is None:
                    password = generate_password()
                    generated = True
                try:
                    validate_password(password, user=user)
                except ValidationError as exc:
                    raise CommandError('Password refused: ' + ' '.join(exc.messages))
                user.set_password(password)
            user.save()

            # The gate to signing in (not is_active - see user/models.py).
            user.profile.email_verified = True
            user.profile.save(update_fields=['email_verified'])

            services.grant(
                user, UserRole.Role.ADMIN, note='Made the admin by create_admin.'
            )

        self.stdout.write(self.style.SUCCESS(
            f'{"Created" if created else "Updated"} the admin: {user.username} <{user.email}>'
        ))
        if generated:
            self.stdout.write(f'Password: {password}')
            self.stdout.write(
                'Shown once. Keep it in a password manager; it is not stored '
                'anywhere readable.'
            )
        elif setting_password:
            self.stdout.write('Password set from ADMIN_PASSWORD.')
        else:
            self.stdout.write('Password unchanged.')
