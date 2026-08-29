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
        Profile.objects.get_or_create(user=instance)
