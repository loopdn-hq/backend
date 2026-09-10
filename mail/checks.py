"""Configuration mistakes the email channel would otherwise hide.

Registered as Django system checks so `manage.py check --deploy` catches them,
which CI already runs as its second gate. Each one is silent at runtime: the
product keeps working, delivers nothing or delivers unsafely, and nothing says
so until somebody asks why they got no email - or why they got somebody else's.
"""
from django.conf import settings
from django.core.checks import Error, Warning, register


@register('mail')
def check_mail_configuration(app_configs, **kwargs):
    """Refuse to deploy an email channel that is quietly wrong."""
    problems = []

    inbound_on = bool(getattr(settings, 'MAIL_REPLY_DOMAIN', ''))
    secret = bool(getattr(settings, 'RESEND_WEBHOOK_SECRET', ''))

    # The important one. The inbound webhook is a public route whose only
    # authentication is the signature, and it posts into private groups. With
    # no secret it accepts anything, so anyone who finds the URL can post as
    # whoever they like - the token in the address is guessable to nobody, but
    # it is also printed in the headers of every email we have ever sent.
    if inbound_on and not secret:
        problems.append(
            Error(
                'MAIL_REPLY_DOMAIN is set but RESEND_WEBHOOK_SECRET is empty, so '
                'the inbound email webhook accepts unsigned requests from anyone.',
                hint='Set RESEND_WEBHOOK_SECRET to the signing secret from the '
                     'provider. Leave it empty only in local development.',
                id='mail.E001',
            )
        )

    # Sending with no provider is not an error - it is the documented
    # log-instead-of-send mode every other backend here uses - but on a real
    # deployment it means nobody is receiving anything and nothing says so.
    if getattr(settings, 'MAIL_SENDING_ENABLED', False) and not settings.RESEND_API_KEY:
        problems.append(
            Warning(
                'MAIL_SENDING_ENABLED is on but RESEND_API_KEY is empty, so group '
                'email is logged and never sent.',
                hint='Set RESEND_API_KEY, or set MAIL_SENDING_ENABLED=False so the '
                     'channel is off deliberately rather than by omission.',
                id='mail.W001',
            )
        )

    # Outbound with no inbound is a usable product - people read in email and
    # reply in the app - but it is worth saying out loud, because the usual
    # cause is that the reply domain was never configured.
    if getattr(settings, 'MAIL_SENDING_ENABLED', False) and not inbound_on:
        problems.append(
            Warning(
                'Group email is on but MAIL_REPLY_DOMAIN is empty, so members can '
                'read messages by email and cannot reply to them.',
                hint='Set MAIL_REPLY_DOMAIN to the domain the inbound provider '
                     'receives on, e.g. reply.loopdn.com.',
                id='mail.W002',
            )
        )

    return problems
