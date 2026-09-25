"""Configuration mistakes that would leave calling quietly broken."""
from django.conf import settings
from django.core.checks import Error, register


@register('calls')
def check_livekit_configuration(app_configs, **kwargs):
    """Refuse to deploy a LiveKit setup that is half there."""
    problems = []

    url = getattr(settings, 'LIVEKIT_URL', '')
    key = getattr(settings, 'LIVEKIT_API_KEY', '')
    secret = getattr(settings, 'LIVEKIT_API_SECRET', '')

    # Any one of the three without the others is a mistake, and the failure is
    # at the far end: a token signed with a wrong or empty secret is refused by
    # LiveKit when somebody connects, and nothing here ever sees it.
    if any((url, key, secret)) and not all((url, key, secret)):
        problems.append(
            Error(
                'LiveKit is partly configured: LIVEKIT_URL, LIVEKIT_API_KEY and '
                'LIVEKIT_API_SECRET must all be set, or none of them.',
                hint='Set all three (the key and secret from AWS Secrets '
                     'Manager), or leave them all blank to run without calls.',
                id='calls.E001',
            )
        )

    return problems
