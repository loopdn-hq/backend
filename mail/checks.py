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

    # Replies post into groups AS the member the From address names, and the
    # only thing that makes a From address hard to forge is the receiving
    # server's SPF/DKIM/DMARC verdict. With the requirement off, anybody who
    # learns a reply address - it is in the headers of every email we have
    # ever sent - can post to that group as that member.
    if inbound_on and not getattr(
        settings, 'MAIL_INBOUND_REQUIRE_AUTHENTICATION', True
    ):
        problems.append(
            Error(
                'MAIL_REPLY_DOMAIN is set but MAIL_INBOUND_REQUIRE_AUTHENTICATION '
                'is off, so a reply is trusted on its From address alone and '
                'anyone holding a reply address can post as that member.',
                hint='Remove MAIL_INBOUND_REQUIRE_AUTHENTICATION or set it to '
                     'True. Turn it off only on a laptop feeding the webhook '
                     'by hand.',
                id='mail.E003',
            )
        )

    # The inbound event is metadata; the reply itself is a second call to the
    # provider that needs the API key. Without the key every reply is fetched,
    # fails, is answered 500 and retried by the provider for a day - and no
    # reply is ever posted, with nothing saying why but a log line per retry.
    if inbound_on and not settings.RESEND_API_KEY:
        problems.append(
            Error(
                'MAIL_REPLY_DOMAIN is set but RESEND_API_KEY is empty, so the '
                'content of a reply can never be fetched and no reply will post.',
                hint='Set RESEND_API_KEY, or leave MAIL_REPLY_DOMAIN empty so '
                     'replies are off deliberately.',
                id='mail.E004',
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

    # The one that closes the release blocker for good. Group email shipped
    # switched off because there was no way for a member to stop it; the way
    # out is a link this service prints into every message, and it cannot build
    # an absolute URL without knowing its own public origin. Without it the
    # channel would ship exactly as broken as before, and silently.
    if getattr(settings, 'MAIL_SENDING_ENABLED', False) and not getattr(
        settings, 'API_PUBLIC_URL', ''
    ):
        problems.append(
            Error(
                'MAIL_SENDING_ENABLED is on but API_PUBLIC_URL is empty, so group '
                'email would go out with no unsubscribe link and no '
                'List-Unsubscribe header.',
                hint="Set API_PUBLIC_URL to this API's public origin, e.g. "
                     'https://api.loopdn.com.',
                id='mail.E002',
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
