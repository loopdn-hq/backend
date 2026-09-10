"""Guarantee every membership has an answer on record for every channel.

Consent is read on the hot path of every fan-out, and a missing row is
indistinguishable from a refusal - so a member created by any route that
forgot to make one would silently stop receiving anything. Memberships arrive
from the REST API, an inbound JOIN text, an email invitation claimed at
registration, the admin, fixtures and the shell. Hanging this off post_save
means none of them can produce a membership the delivery engine will not
speak to.

The default is the answer the old single column encoded:

    an account holder   opted in - they agreed in their own settings, so there
                        was never a group question to ask
    phone or email only pending - somebody put their number or address in a
                        group, and silence is not agreement

That distinction is the product's consent story, not a convenience: texting a
number nobody confirmed is the thing carriers suspend campaigns for.

The default is where the two channels stop agreeing, and it is worth being
explicit about why. A row that later gains an account is upgraded for EMAIL and
never for SMS - see `chat/membership.py:claim_email_invitations`. Verifying an
address proves the person reading it is the person who was invited, so there is
nothing left to ask about email. Nobody has confirmed the phone number by text,
so for SMS there still is.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from chat.models import ChannelConsent, Participant


@receiver(
    post_save, sender=Participant, dispatch_uid='chat.seed_channel_consent'
)
def seed_channel_consent(sender, instance, created, **kwargs):
    if not created:
        return

    default = _default_status(instance)
    for channel in (
        ChannelConsent.Channel.SMS,
        ChannelConsent.Channel.EMAIL,
    ):
        ChannelConsent.objects.get_or_create(
            participant=instance,
            channel=channel,
            defaults={'status': default},
        )


def _default_status(participant):
    if participant.user_id is None:
        return ChannelConsent.Status.PENDING
    return ChannelConsent.Status.OPTED_IN
