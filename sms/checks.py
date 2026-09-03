"""Configuration mistakes that would otherwise be discovered in production.

Registered as Django system checks, so `manage.py check --deploy` catches them -
which CI already runs as its second gate. Each one here is a mistake that is
SILENT at runtime: the product keeps working, does the wrong thing, and nothing
in a log says so until a bill or a breach turns up.
"""
from django.conf import settings
from django.core.checks import Error, Warning, register


@register('sms')
def check_sms_configuration(app_configs, **kwargs):
    """Refuse to deploy an SMS setup that is quietly wrong."""
    problems = []

    messaging_on = bool(getattr(settings, 'TELNYX_MESSAGING_PROFILE_ID', ''))
    public_keys = getattr(settings, 'TELNYX_PUBLIC_KEYS', [])

    # The important one. The inbound webhook is the only route in this project
    # that any machine on the internet can reach, and its ONLY authentication is
    # the signature. With no public keys configured it accepts anything - so
    # anyone who finds the URL can post messages into private groups as whoever
    # they like, and trigger billed fan-out while doing it.
    if messaging_on and not public_keys:
        problems.append(
            Error(
                'Group SMS is configured but TELNYX_PUBLIC_KEYS is empty, so the '
                'inbound webhook accepts unsigned requests from anyone.',
                hint='Set TELNYX_PUBLIC_KEYS (comma-separated; two during a key '
                     'rotation). Leave it empty only in local development.',
                id='sms.E001',
            )
        )

    # Sending without a provider is not an error - that is the documented
    # log-instead-of-send mode every other backend here uses - but on a real
    # deployment it means nobody is receiving anything and nothing says so.
    if getattr(settings, 'SMS_SENDING_ENABLED', False) and not messaging_on:
        problems.append(
            Warning(
                'SMS_SENDING_ENABLED is on but no messaging provider is '
                'configured, so outbound messages are logged and never sent.',
                hint='Set TELNYX_MESSAGING_PROFILE_ID, or set '
                     'SMS_SENDING_ENABLED=False to make the intent explicit.',
                id='sms.W001',
            )
        )

    # Per-process cache means per-process counters: with two web workers the
    # real ceiling is double what the setting says, and it drifts with the
    # replica count.
    if messaging_on and not getattr(settings, 'REDIS_URL', ''):
        problems.append(
            Warning(
                'Group SMS is configured without REDIS_URL, so spend ceilings '
                'are counted per process and are multiplied by the number of '
                'workers.',
                hint='Set REDIS_URL so every worker shares one budget.',
                id='sms.W002',
            )
        )

    if getattr(settings, 'SMS_MAX_SEGMENTS_PER_DAY', 0) <= 0:
        problems.append(
            Error(
                'SMS_MAX_SEGMENTS_PER_DAY must be a positive number; there is '
                'no safe "unlimited" for a path that spends money.',
                id='sms.E002',
            )
        )

    return problems
