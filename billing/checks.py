"""Configuration mistakes that would otherwise be discovered in production."""
from django.conf import settings
from django.core.checks import Error, register


@register('billing')
def check_stripe_configuration(app_configs, **kwargs):
    """Refuse to deploy a Stripe setup that is quietly wrong."""
    problems = []

    secret_key = getattr(settings, 'STRIPE_SECRET_KEY', '')
    webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')

    # The webhook endpoint is public, and stripe.Webhook.construct_event skips
    # signature verification entirely when handed an empty secret - so an
    # unset STRIPE_WEBHOOK_SECRET means anyone who finds the URL can post
    # fabricated events (e.g. a fake "payment succeeded") as Stripe.
    if secret_key and not webhook_secret:
        problems.append(
            Error(
                'STRIPE_SECRET_KEY is configured but STRIPE_WEBHOOK_SECRET is '
                'empty, so the webhook endpoint cannot verify events came from '
                'Stripe.',
                hint='Set STRIPE_WEBHOOK_SECRET to the signing secret from the '
                     'Stripe CLI (`stripe listen`) or the dashboard webhook '
                     'endpoint.',
                id='billing.E001',
            )
        )

    return problems
