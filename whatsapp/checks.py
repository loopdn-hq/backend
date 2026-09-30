"""WhatsApp configuration mistakes that would otherwise surface in production."""
from django.conf import settings
from django.core.checks import Error, Warning, register


@register('whatsapp')
def check_whatsapp_configuration(app_configs, **kwargs):
    problems = []
    app_id = getattr(settings, 'WHATSAPP_APP_ID', '')
    secret = getattr(settings, 'WHATSAPP_APP_SECRET', '')

    if app_id and not secret:
        problems.append(Error(
            'WHATSAPP_APP_ID is set without WHATSAPP_APP_SECRET, so the webhook '
            'accepts unsigned requests from anyone.',
            hint='Set WHATSAPP_APP_SECRET to the Meta app secret.',
            id='whatsapp.E001',
        ))

    if app_id and not getattr(settings, 'WHATSAPP_WEBHOOK_VERIFY_TOKEN', ''):
        problems.append(Warning(
            'WHATSAPP_WEBHOOK_VERIFY_TOKEN is empty, so Meta cannot verify the '
            'webhook URL and no events will arrive.',
            id='whatsapp.W001',
        ))

    # Tokens encrypted with a key derived from SECRET_KEY become unreadable the
    # day SECRET_KEY rotates - every tenant silently disconnected at once.
    if app_id and not settings.DEBUG and not getattr(settings, 'WHATSAPP_TOKEN_KEYS', []):
        problems.append(Error(
            'WHATSAPP_TOKEN_KEYS is empty, so business tokens are encrypted with '
            'a key derived from SECRET_KEY.',
            hint='Generate a key with cryptography.fernet.Fernet.generate_key().',
            id='whatsapp.E002',
        ))

    if app_id and not getattr(settings, 'WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID', ''):
        problems.append(Warning(
            'WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID is empty, so nobody can connect a number.',
            id='whatsapp.W002',
        ))
    return problems
