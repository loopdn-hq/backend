"""Switch campaigns being shown in chat on or off, for the whole environment.

    python manage.py campaign_serving              # says whether it is on
    python manage.py campaign_serving on --note "Launch in PH"
    python manage.py campaign_serving off --note "A card went somewhere wrong"

The one switch (AdSettings): it ships off, and with it off nothing is served,
whatever is running. Turning it off takes effect within a minute everywhere.
The note says why, for whoever finds it later. An admin-site toggle can come
later; until then this is the way, once per environment.
"""
from django.core.management.base import BaseCommand

from commercial.models import AdSettings


class Command(BaseCommand):
    help = 'Switch campaigns being shown in chat on or off.'

    def add_arguments(self, parser):
        parser.add_argument('state', nargs='?', choices=['on', 'off'])
        parser.add_argument('--note', default='', help='Why, for whoever finds it later.')

    def handle(self, *args, state=None, note='', **options):
        row, _ = AdSettings.objects.get_or_create(singleton=True)
        if state is not None:
            row.serving_enabled = state == 'on'
            row.note = note[:200]
            row.save()
        self.stdout.write(
            f'Serving is {"ON" if row.serving_enabled else "OFF"}'
            + (f' - {row.note}' if row.note else '')
        )
