"""Guarantee every User has a Profile, MessagingSettings and an account role.

Registration creates them explicitly, but users also arrive from
`createsuperuser`, the admin, fixtures and the shell. Hanging this off
post_save means no code path anywhere can produce a user whose
`user.profile` raises RelatedObjectDoesNotExist, or who holds no role at all.
"""
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from user.models import MessagingSettings, Profile


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
        # The account-wide SMS and email switches live here, and the delivery
        # code reads them for every member of a group - so the row exists from
        # the start, not made lazily in the middle of a send.
        MessagingSettings.objects.get_or_create(user=instance)

        # The baseline account role. Stored rather than implied by an absent
        # row, so that having NO rows stays a detectable bug rather than a
        # state something has to interpret - see rbac/models.py.
        #
        # Imported here rather than at module level: this module is imported
        # from user/apps.py:ready(), and a top-level import of another app's
        # models runs before the registry is populated.
        #
        # `admin` is deliberately NOT granted alongside is_superuser. The
        # backfill in rbac/0003 was a one-time bootstrap; syncing the two on
        # every save would rebuild the coupling rbac exists to remove, and
        # would mean a Django-admin account silently carried every product
        # capability.
        from rbac.models import UserRole

        UserRole.objects.get_or_create(
            user=instance, role=UserRole.Role.STANDARD
        )
