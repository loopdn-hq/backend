"""Configuration mistakes that would leave push quietly doing nothing.

Registered as Django system checks, which `manage.py check --deploy` runs in CI
already. Every failure mode here is silent at runtime: the product works, the
switches on the Notifications screen move, and no notification ever arrives.
"""
from django.conf import settings
from django.core.checks import Error, Warning, register


@register('push')
def check_push_configuration(app_configs, **kwargs):
    """Refuse to deploy a push channel that cannot send."""
    problems = []

    enabled = getattr(settings, 'PUSH_SENDING_ENABLED', False)
    public = getattr(settings, 'VAPID_PUBLIC_KEY', '')
    private = getattr(settings, 'VAPID_PRIVATE_KEY', '')
    subject = getattr(settings, 'VAPID_SUBJECT', '')

    # The switch on with no keys is the one that looks like it works. The web
    # app asks for a key, gets an empty string, and every browser refuses to
    # subscribe with an error that never reaches us.
    if enabled and not (public and private):
        problems.append(
            Error(
                'PUSH_SENDING_ENABLED is on but the VAPID key pair is '
                'incomplete, so no browser can subscribe and nothing is sent.',
                hint='Run `python manage.py generate_vapid_keys` and set '
                     'VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY, or set '
                     'PUSH_SENDING_ENABLED=False so push is off deliberately.',
                id='push.E001',
            )
        )

    # Half a pair is worse than none: the keys must be two halves of the SAME
    # pair, and a deployment holding one of them is a deployment where somebody
    # copied one value and not the other.
    if bool(public) != bool(private):
        problems.append(
            Error(
                'Only one half of the VAPID key pair is set. The public and '
                'private keys must come from the same pair or every send is '
                'rejected by the browser\'s push service.',
                hint='Generate both with `python manage.py generate_vapid_keys` '
                     'and set them together.',
                id='push.E002',
            )
        )

    # The push services want a way to contact whoever is sending. Mozilla is
    # the strict one and refuses a claim that is not a mailto: or https: URL.
    if enabled and not subject.startswith(('mailto:', 'https://')):
        problems.append(
            Warning(
                'VAPID_SUBJECT is not a mailto: or https:// URL, which some '
                'push services refuse.',
                hint='Set VAPID_SUBJECT to something like '
                     'mailto:support@loopdn.com.',
                id='push.W001',
            )
        )

    return problems
