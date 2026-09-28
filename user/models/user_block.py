"""One person blocking another - Settings → Privacy → Blocked users."""
from django.conf import settings
from django.db import models

from .profile import phone_number_validator


class UserBlock(models.Model):
    """One person's decision not to hear from another, person to person.

    NOT `chat.Participant.Status.BLOCKED`, and the two must never be confused.
    That one is an ADMIN removing somebody from ONE conversation, and it takes
    their membership away - a blocked participant disappears from the member
    list entirely. This one takes no membership from anybody: both people stay
    in every group they share, both keep seeing each other's group messages,
    and both stay visible to each other in the member list. Only DIRECT contact
    stops.

    Two different rules that happen to share a word. When they meet in the same
    payload, this one is never called `blocked`.

    SYMMETRIC WHEN IT IS READ, ONE-SIDED WHEN IT IS WRITTEN. A row records that
    `blocker` blocked somebody; `blocks_between` returns True if EITHER side
    has a row against the other. That asymmetry is deliberate - the spec asks
    for it (§16) and it is what stops the block being detectable: if a block
    only worked one way, B could discover A had blocked them by observing that
    their own block behaved differently.

    A NUMBER IS A BLOCKABLE THING. Most members of most groups have no account
    at all - `chat.Participant.user` is nullable and a phone number is the only
    identity many of them have - so a block keyed solely on User could not say
    "not that person" about most of the people in the product. Exactly one of
    the two target columns is set, which is the same shape, and the same
    reasoning, as `Participant` itself.
    """

    blocker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='blocks_made',
    )

    # Exactly one of these two. See the class docstring.
    blocked_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='blocks_against',
        null=True,
        blank=True,
    )
    blocked_phone_number = models.CharField(
        max_length=16,
        null=True,
        blank=True,
        validators=[phone_number_validator],
    )

    # The third shape a person can have here, for the same reason the second
    # one exists: most people an email conversation reaches have no account, so
    # a block keyed only on accounts and numbers could not name them at all.
    blocked_email = models.EmailField(max_length=254, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # One row per pair. A second would make unblocking ambiguous -
            # delete which? - and buys nothing, since the rule is a boolean.
            models.UniqueConstraint(
                fields=['blocker', 'blocked_user'],
                condition=models.Q(blocked_user__isnull=False),
                name='unique_user_block',
            ),
            models.UniqueConstraint(
                fields=['blocker', 'blocked_phone_number'],
                condition=models.Q(blocked_phone_number__isnull=False),
                name='unique_phone_block',
            ),
            models.UniqueConstraint(
                fields=['blocker', 'blocked_email'],
                condition=models.Q(blocked_email__isnull=False),
                name='unique_email_block',
            ),
            # Exactly one target, never two and never none. More than one set
            # is the dangerous state: the rule would have two answers for one
            # row. Three shapes now, and the reasoning is unchanged.
            models.CheckConstraint(
                condition=(
                    models.Q(blocked_user__isnull=False,
                             blocked_phone_number__isnull=True,
                             blocked_email__isnull=True)
                    | models.Q(blocked_user__isnull=True,
                               blocked_phone_number__isnull=False,
                               blocked_email__isnull=True)
                    | models.Q(blocked_user__isnull=True,
                               blocked_phone_number__isnull=True,
                               blocked_email__isnull=False)
                ),
                name='block_identifies_someone',
            ),
            models.CheckConstraint(
                condition=~models.Q(blocker=models.F('blocked_user')),
                name='block_is_not_self',
            ),
        ]
        indexes = [
            # The reverse direction. The unique constraints above already index
            # (blocker, ...), which answers "did I block them"; this answers
            # "did they block me", and the symmetric read asks both every time.
            models.Index(fields=['blocked_user']),
            models.Index(fields=['blocked_phone_number']),
            models.Index(fields=['blocked_email']),
        ]

    def __str__(self):
        target = (
            self.blocked_user or self.blocked_phone_number or self.blocked_email
        )
        return f'{self.blocker} blocked {target}'
