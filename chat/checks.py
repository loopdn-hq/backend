"""Configuration mistakes in the realtime layer that are silent at runtime.

Registered as Django system checks so `manage.py check --deploy` catches them -
the second CI gate. The one below is the kind of mistake that does not raise, does
not log, and does not fail a health check: everything keeps working for whoever
happens to share a process with the sender.
"""
from django.conf import settings
from django.core.checks import Error, register


@register('chat')
def check_channel_layer(app_configs, **kwargs):
    """Refuse to deploy realtime on a per-process channel layer.

    With REDIS_URL unset, settings falls back to InMemoryChannelLayer. That
    layer is per PROCESS, so on more than one ECS task:

        the socket connects to task A
        somebody posts through REST, load-balanced to task B
        task B broadcasts on ITS layer, which task A cannot see
        the first user receives nothing

    It presents as "realtime works, then stops, and refreshing fixes it" -
    refreshing lands the socket on a different task. It is invisible from the
    server: `send_to_group` guards only against the layer being None, and
    InMemoryChannelLayer is not None, so every broadcast reports success.

    An Error rather than a Warning because there is no correct multi-task
    deployment without a shared layer, and nothing else in the system will ever
    tell you.
    """
    if settings.DEBUG:
        # One process, one layer - which is the whole point of the fallback.
        return []

    # Keyed on the configured DATABASE, following the same environment-keyed
    # convention as the rest of settings: POSTGRES_DB selects Postgres, and a
    # real deployment always has one. CI runs `check --deploy` with DEBUG off
    # and neither POSTGRES_DB nor REDIS_URL, deliberately, and falls back to
    # SQLite - so without this guard every build would fail on a condition that
    # is only ever true of a deployment.
    engine = settings.DATABASES.get('default', {}).get('ENGINE', '')
    if 'postgresql' not in engine:
        return []

    backend = (
        settings.CHANNEL_LAYERS.get('default', {}).get('BACKEND', '')
        if getattr(settings, 'CHANNEL_LAYERS', None)
        else ''
    )
    if 'InMemory' not in backend:
        return []

    return [
        Error(
            'The channel layer is InMemoryChannelLayer, which is per process, '
            'so websocket messages are only delivered to clients that happen '
            'to share a process with the sender.',
            hint='Set REDIS_URL so every task shares one layer. With one task '
                 'this merely works by accident; with two it drops messages '
                 'silently and looks like a flaky socket.',
            id='chat.E001',
        )
    ]
