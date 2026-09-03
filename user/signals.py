"""Guarantee every User has a Profile.

Registration creates one explicitly, but users also arrive from
`createsuperuser`, the admin, fixtures and the shell. Hanging this off
post_save means no code path anywhere can produce a user whose
`user.profile` raises RelatedObjectDoesNotExist.
"""
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from user.models import Profile


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid='user.create_profile')
def create_profile_for_new_user(sender, instance, created, **kwargs):
    if created:
        # A superuser is created by whoever has the database, not through
        # registration, so there is no address to confirm and nobody to email.
        # Without this the account exists but cannot use the API at all -
        # LoginSerializer refuses every unverified account, and the two
        # verification endpoints are the only cure. Deliberately narrow: an
        # ordinary user made in the shell or a fixture stays unverified,
        # because nobody has confirmed their address either.
        Profile.objects.get_or_create(
            user=instance,
            defaults={'email_verified': instance.is_superuser},
        )
