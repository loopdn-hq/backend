"""Guarantee every membership has an answer on record for every channel.

Consent is read on the hot path of every fan-out, and a missing row is
indistinguishable from a refusal - so a member created by any route that
forgot to make one would silently stop receiving anything. Memberships arrive
from the REST API, an inbound JOIN text, an email invitation claimed at
registration, the admin, fixtures and the shell. Hanging this off post_save
means none of them can produce a membership the delivery engine will not
speak to.

The default is the answer the old single column encoded:

    an account holder   opted in - they agreed to SMS in their own settings,
                        so there was never a group question to ask
    phone-only          pending - somebody put their number in a group, and
                        silence is not agreement

That distinction is the product's consent story, not a convenience: texting a
number nobody confirmed is the thing carriers suspend campaigns for.
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

    ChannelConsent.objects.get_or_create(
        participant=instance,
        channel=ChannelConsent.Channel.SMS,
        defaults={'status': _default_sms_status(instance)},
    )


def _default_sms_status(participant):
    if participant.user_id is None:
        return ChannelConsent.Status.PENDING
    return ChannelConsent.Status.OPTED_IN
