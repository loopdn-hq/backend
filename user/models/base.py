"""What every per-user settings model shares.

One model per settings screen, one row per user, so a screen's fields live
together and a new screen is a new file rather than more columns on Profile.

Rows are created when the user signs in (user/user_settings.py), not by a
post_save signal, so an account that never verifies gets none. `for_user`
creates a missing row on first use for anything that meets a user before
that - so there is no data migration, and no code path that can meet a user
without settings.
"""
from django.db import models


class UserSettings(models.Model):
    """Timestamps and `for_user`. Each subclass declares its own `user`
    one-to-one, because a related_name has to be unique per model."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    @classmethod
    def for_user(cls, user):
        """The user's row, created with the defaults if there is none yet.

        get_or_create retries the read if two requests race to create the row,
        so the one-to-one can never end up with two.
        """
        row, _ = cls.objects.get_or_create(user=user)
        return row

    @classmethod
    def of(cls, user):
        """The user's row for READING, without ever writing one.

        For code that asks about many people at once - a fan-out over a group's
        members, a page of a member list - where `for_user` would insert a row
        per person it merely asked about.

        Every account gets its rows at sign-in; if one is somehow missing the
        answer is the defaults, and the defaults are right, because everything
        that changes a setting creates the row it writes.
        """
        try:
            return getattr(user, cls._meta.get_field('user').remote_field.get_accessor_name())
        except cls.DoesNotExist:
            return cls(user=user)

    def __str__(self):
        return f'{type(self).__name__}<{self.user.username}>'
