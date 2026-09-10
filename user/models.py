"""Profile data hanging off Django's built-in User.

The project uses `django.contrib.auth.models.User` as-is. Everything the
messaging app needs beyond a username and password lives here, in a
one-to-one Profile, so that Django's auth, admin and password machinery keep
working untouched.
"""
import uuid
from pathlib import Path

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


def avatar_upload_to(instance, filename):
    """Store avatars under a per-user folder with an unguessable name.

    The original filename is discarded on purpose: it is attacker-controlled,
    and users routinely upload `IMG_0001.jpg`, which would collide.
    """
    suffix = Path(filename).suffix.lower() or '.jpg'
    return f'avatars/{instance.user_id}/{uuid.uuid4().hex}{suffix}'


# E.164: a leading +, a non-zero country digit, then 7-14 more digits. Stored
# in this one canonical form so a lookup by phone number is an exact match.
phone_number_validator = RegexValidator(
    regex=r'^\+[1-9]\d{7,14}$',
    message='Enter the number in international format, e.g. +639171234567.',
)


class Profile(models.Model):
    """Everything about a person that is not a credential."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
    )

    # What other people see in a conversation list. Falls back to the
    # username when blank - see `display_label` below.
    display_name = models.CharField(max_length=80, blank=True)

    avatar = models.ImageField(
        upload_to=avatar_upload_to,
        blank=True,
        null=True,
    )

    # Nullable rather than blank-only: unique refuses more than one '' but
    # allows any number of NULLs, and most users will not set a number.
    phone_number = models.CharField(
        max_length=16,
        unique=True,
        null=True,
        blank=True,
        validators=[phone_number_validator],
        help_text='International format, e.g. +639171234567.',
    )
    # Set only by the verification flow (an SMS code today, a provider
    # callback later) - never by the profile endpoint, or "verified" would
    # mean nothing. Reset automatically in save() when the number changes.
    phone_no_verified = models.BooleanField(default=False)

    # The master switch for every text this account could receive, across every
    # group. It only ever SUBTRACTS: turning it on does not opt anybody into
    # anything, because whether a given group texts you is decided per
    # membership on `chat.Participant.sms_enabled`. The effective rule is
    #
    #     send = participant.sms_enabled and profile.sms_enabled
    #
    # so this is the one control that stops everything at once - which is why
    # it defaults to True and is written from the profile endpoint, while the
    # per-group flags default to False and are opted into individually.
    #
    # Members who have no account have no Profile and therefore no switch here:
    # replying STOP is their only way out, which is why STOP handling is not
    # optional.
    sms_enabled = models.BooleanField(default=True, db_default=True)
    # When they last turned it off, or replied STOP. Kept apart from the flag
    # because "has never opted out" and "opted out and back in" are different
    # facts, and a complaint turns on the second one.
    sms_opt_out_at = models.DateTimeField(null=True, blank=True)
    sms_consent_at = models.DateTimeField(null=True, blank=True)

    # Whether `user.email` has been confirmed with an emailed code. This is
    # the flag that gates signing in, not `user.is_active`.
    #
    # The two are kept strictly apart, and the split is load-bearing:
    #
    #   is_active      an admin has not banned this account. Written by admins
    #                  only - never by the verification flow.
    #   email_verified the address has been confirmed. Written by the
    #                  verification flow only - never by the profile endpoint.
    #
    # Collapsing them, which is the obvious shortcut, breaks bans: a banned
    # account and a never-verified one become indistinguishable, so the two
    # public verification endpoints hand a banned user a way to switch their
    # own account back on. Cleared again in UserSerializer.update when the
    # address changes, which is why is_active must not follow it.
    email_verified = models.BooleanField(default=False)

    # The same master switch as `sms_enabled`, for group email, and it works
    # the same way: it only ever subtracts. Whether a given group emails you is
    # decided per membership by `chat.ChannelConsent`; this is the one control
    # that stops every group at once.
    #
    # Only group traffic. Verification codes and password resets are not
    # subject to it - an account that cannot receive the code to get back into
    # itself is an account somebody has locked themselves out of, and nobody
    # who unticks "email updates" means that.
    email_enabled = models.BooleanField(default=True, db_default=True)

    bio = models.CharField(max_length=280, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)

    # Presence, for the conversation list. `is_online` is the live flag the
    # websocket layer will flip; `last_seen_at` is what to show once it is
    # False.
    is_online = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['is_online']),
        ]

    def __str__(self):
        return f'Profile<{self.user.username}>'

    def save(self, *args, **kwargs):
        # A blank number arriving from a form would be '' and collide with
        # every other blank one under the unique constraint.
        if not self.phone_number:
            self.phone_number = None

        # Changing the number must drop the verified flag, otherwise someone
        # verifies a number they control, swaps in one they do not, and keeps
        # the badge.
        #
        # The exception is a caller that sets the number and the flag in the
        # same write - that is the verification flow itself confirming a new
        # number, and resetting it there would make verification impossible.
        if self.pk and self.phone_no_verified and self._phone_number_changed():
            self.phone_no_verified = False
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                # A caller naming its fields would otherwise not persist the
                # reset we just made.
                kwargs['update_fields'] = set(update_fields) | {'phone_no_verified'}

        super().save(*args, **kwargs)
        self._snapshot_phone_state()

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._snapshot_phone_state()

    def _snapshot_phone_state(self):
        """Record the stored phone state as the baseline for the next save.

        Skipped when either field is deferred: reading a deferred attribute
        calls refresh_from_db(), which lands back here and recurses. Leaving
        the snapshot unset is safe - _phone_number_changed() treats a missing
        baseline as "nothing to compare", so it never resets the flag on a
        guess.
        """
        deferred = self.get_deferred_fields()
        if 'phone_number' in deferred or 'phone_no_verified' in deferred:
            return

        self._loaded_phone_number = self.phone_number
        self._loaded_phone_no_verified = self.phone_no_verified

    @classmethod
    def from_db(cls, db, field_names, values, **kwargs):
        """Remember what was loaded, so save() can see what changed.

        **kwargs is passed straight through: Django adds arguments here
        between releases (`fetch_mode` in 6.1), and a narrower signature
        breaks every query on upgrade.
        """
        instance = super().from_db(db, field_names, values, **kwargs)
        instance._snapshot_phone_state()
        return instance

    def _phone_number_changed(self):
        """True when this save alters the number but not the verified flag."""
        # An instance that never came from the database (or came from a
        # deferred query) has nothing to compare against; leave it alone.
        if not hasattr(self, '_loaded_phone_number'):
            return False
        if self.phone_no_verified != self._loaded_phone_no_verified:
            # The flag is being written in this same save - the caller is the
            # verification flow, so it wins.
            return False
        return self._loaded_phone_number != self.phone_number

    @property
    def display_label(self):
        """The name to render, never empty."""
        return self.display_name or self.user.get_full_name() or self.user.username
