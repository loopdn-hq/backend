"""Who has blocked whom, and what that stops.

One entry point per action, for the reason `chat/membership.py` has one: the
REST endpoints, the direct-conversation serializer and the message write path
all have to ask the same question, and three copies of "or the other way round"
is three chances to write it one-way by accident.

WHAT A BLOCK STOPS, IN THIS RELEASE
-----------------------------------
Direct contact only.

    starting a direct conversation   refused, both directions
    sending into an existing one     refused, both directions
    inviting them to a group         refused
    finding them in user search      they do not appear

A block may name an account OR a bare phone number, and the two are the same
person as soon as that number is verified against that account - see
`link_number_to_account`, which is what stops signing up being a way round one.

Nothing about a group the two people already share changes: both stay members,
both keep seeing each other's messages in the app, and both stay in the member
list. What DOES stop inside a shared group is the external notification - a
text, an email, later a push or a voice alert - caused by somebody they
blocked. The message is still there to read; it just does not come and find
them. `screen_for_sender` below is how the fan-out asks.

READ SYMMETRIC, WRITE ONE-SIDED
-------------------------------
`blocks_between` is true if EITHER person has blocked the other. A block that
only worked in one direction would be detectable: B would notice their own
messages behaving differently from A's, which is precisely what the generic
refusals below exist to prevent.

NOTHING HERE TELLS ANYBODY A BLOCK EXISTS
-----------------------------------------
Callers refuse with whatever they would say if the other person simply were not
there. The refusal copy lives at the call sites, not here, because each one has
a different "nothing to see" to imitate - a search that found nobody, a
username that resolves to nothing, a conversation that will not take a message.
"""
import logging
from dataclasses import dataclass, field

from django.db.models import Q

from user.models import UserBlock

logger = logging.getLogger(__name__)


def block(blocker, *, user=None, phone_number=None, email=None):
    """Record that `blocker` does not want contact. Idempotent.

    Exactly one of `user`, `phone_number` or `email` names the other party,
    matching the model. Blocking twice is one row and not an error: the caller
    is a person pressing a button, and the second press means the same as the
    first.
    """
    _one_target(user, phone_number, email)
    address = _normalised_email(email)

    if user is not None and user.pk == blocker.pk:
        # The database refuses this too, but a constraint error is a 409 and
        # this is simply a request that means nothing.
        raise ValueError('You cannot block yourself.')

    if address is not None and address == _normalised_email(blocker.email):
        # The same request, by the other name. No constraint catches this one -
        # the column holds an address, not an id - so it has to be refused here
        # or somebody blocks themselves and cannot work out why nothing arrives.
        raise ValueError('You cannot block yourself.')

    row, _ = UserBlock.objects.get_or_create(
        blocker=blocker,
        blocked_user=user,
        blocked_phone_number=_normalised(phone_number),
        blocked_email=address,
    )
    return row


def unblock(blocker, *, user=None, phone_number=None, email=None):
    """Undo it. Returns True if there was anything to undo."""
    _one_target(user, phone_number, email)

    deleted, _ = UserBlock.objects.filter(
        blocker=blocker,
        blocked_user=user,
        blocked_phone_number=_normalised(phone_number),
        blocked_email=_normalised_email(email),
    ).delete()
    return bool(deleted)


def has_blocked(blocker, other):
    """One direction: did THIS person block that one.

    `blocks_between` is the right question almost everywhere, because a block
    must behave the same from both sides or it is detectable. This one exists
    for the one place where the two sides SHOULD differ - what each is told.

    The blocker already knows they blocked somebody, so telling them costs no
    privacy and saves them a confusing dead end. The blocked person must learn
    nothing, so they get the answer a stranger would get. Asking this rather
    than `blocks_between` is how a caller tells those two people apart.
    """
    if blocker is None or other is None:
        return False

    return UserBlock.objects.filter(
        blocker=blocker, blocked_user=other
    ).exists()


def blocks_between(one, other):
    """Whether EITHER of these two accounts has blocked the other.

    The question every caller actually has. Both arguments are Users; a caller
    holding a phone number wants `blocks_number` instead.
    """
    if one is None or other is None:
        return False
    if one.pk == other.pk:
        return False

    return UserBlock.objects.filter(
        Q(blocker=one, blocked_user=other) | Q(blocker=other, blocked_user=one)
    ).exists()


def blocks_number(blocker, phone_number):
    """Whether `blocker` has blocked this bare number.

    One direction only, and that is not an oversight: a number with no account
    has nobody who could have done the blocking. When it later becomes an
    account, whether the block follows it is an open product question - see
    docs/loopdn_user_blocking_behavior_spec.md.
    """
    number = _normalised(phone_number)
    if blocker is None or not number:
        return False

    return UserBlock.objects.filter(
        blocker=blocker, blocked_phone_number=number
    ).exists()


def blocks_email(blocker, email):
    """Whether `blocker` has blocked this bare address.

    One direction only, for exactly the reason `blocks_number` is: an address
    with no account behind it has nobody who could have done the blocking.
    `link_email_to_account` is what happens when that stops being true.
    """
    address = _normalised_email(email)
    if blocker is None or not address:
        return False

    return UserBlock.objects.filter(
        blocker=blocker, blocked_email=address
    ).exists()


def link_number_to_account(user):
    """Move any block on this account's number onto the account itself.

    Called the first time a number is VERIFIED against an account, which is the
    moment a bare number stops being an anonymous handset and becomes a person.

    THE ROW MOVES, IT IS NOT COPIED, and that is the whole rule. Somebody who
    blocked a number blocked whoever was answering it. Once that person has an
    account, the block is about them:

        before   blocker -> +639171234567
        after    blocker -> that account, and nothing against the number

    So a carrier reassigning the number later hands a stranger a clean line -
    which is right. They are not the person anybody blocked, and leaving the
    number blocked would punish them for inheriting a handset.

    Without this, registering an account would have been a way straight back in
    - block the number, they sign up, and the block names an identity nobody is
    using any more. It is the same hole `chat/models.py:_claim_phone_participant`
    already refuses to leave open for the conversation-level block.

    Returns how many blocks moved. Safe to call twice: the second run finds
    nothing, because the first left no row against the number.
    """
    profile = getattr(user, 'profile', None)
    number = _normalised(getattr(profile, 'phone_number', None))
    if number is None or not getattr(profile, 'phone_no_verified', False):
        return 0

    moved = 0
    for row in UserBlock.objects.filter(blocked_phone_number=number):
        if row.blocker_id == user.pk:
            # They blocked their own number - which people do, by typo or by
            # blocking a number they later claim. A self-block is refused by a
            # constraint and means nothing, so it goes rather than converts.
            row.delete()
            continue

        if UserBlock.objects.filter(
            blocker_id=row.blocker_id, blocked_user=user
        ).exists():
            # This blocker already blocked the account by name. Two rows for
            # one person is the ambiguity the model exists to prevent - and it
            # would make unblocking a guess about which one to delete.
            row.delete()
            continue

        row.blocked_user = user
        row.blocked_phone_number = None
        row.save(update_fields=['blocked_user', 'blocked_phone_number'])
        moved += 1

    if moved:
        logger.info(
            'Moved %s phone block(s) onto account %s after verification',
            moved, user.pk,
        )
    return moved


def link_email_to_account(user):
    """Move any block on this account's address onto the account itself.

    The twin of `link_number_to_account`, called the first time an address is
    VERIFIED against an account - the moment a bare address stops being a
    mailbox and becomes a person. The rule is the same one, for the same
    reason:

        before   blocker -> stewart@gmail.com
        after    blocker -> that account, and nothing against the address

    Without it, signing up would be a way straight back in: block the address,
    they register it, and the block names an identity nobody is using.

    Returns how many blocks moved. Safe to call twice - the second run finds
    nothing, because the first left no row against the address.
    """
    profile = getattr(user, 'profile', None)
    address = _normalised_email(getattr(user, 'email', None))
    if address is None or not getattr(profile, 'email_verified', False):
        return 0

    moved = 0
    for row in UserBlock.objects.filter(blocked_email=address):
        if row.blocker_id == user.pk:
            # They blocked their own address. A self-block means nothing, so it
            # goes rather than converts - and the account form of it is refused
            # by a constraint anyway.
            row.delete()
            continue

        if UserBlock.objects.filter(
            blocker_id=row.blocker_id, blocked_user=user
        ).exists():
            # Already blocked by name. Two rows for one person is the ambiguity
            # the model exists to prevent, and would make unblocking a guess.
            row.delete()
            continue

        row.blocked_user = user
        row.blocked_email = None
        row.save(update_fields=['blocked_user', 'blocked_email'])
        moved += 1

    if moved:
        logger.info(
            'Moved %s email block(s) onto account %s after verification',
            moved, user.pk,
        )
    return moved


@dataclass(frozen=True)
class Screen:
    """Who must not be told about one sender: accounts, and bare addresses.

    Two sets rather than one because a recipient is not always an account.
    `users` is symmetric - either party having blocked the other puts an id in
    it - while `emails` can only ever hold addresses THIS sender blocked, since
    an address with no account behind it has nobody who could have blocked back.

    Falsy when it screens nobody, so `if not screen` still short-circuits a
    fan-out the way it did when this was a frozenset.
    """

    users: frozenset = field(default_factory=frozenset)
    emails: frozenset = field(default_factory=frozenset)

    def __bool__(self):
        return bool(self.users or self.emails)


def screen_for_sender(*, user_id=None, phone_number=None, email=None):
    """Everybody who must NOT be told about this sender. One query.

    The bulk form of `blocks_between`, for the delivery path. A fan-out asks
    this question once for a whole group rather than once per member: asked
    per recipient it is one round trip each, which for a two-hundred-member
    group is two hundred - and then two hundred again when the email channel
    fans out the same message.

    Returns a frozenset of account ids, so the caller matches it against the
    `user_id` it has already loaded. Empty when the sender has neither
    identity, which is a system message - nobody caused it, so nobody is
    screened from it.

    THREE WAYS SOMEBODY LANDS IN THE SET, and the asymmetry is not an oversight:

        sender is an account   they blocked the recipient, OR the recipient
                               blocked them - symmetric, per S16, because a
                               block that worked one way would be detectable
        sender is a number     only "the recipient blocked that number", since
                               `blocker` is a non-nullable FK to User and
                               somebody with no account cannot have blocked
                               anyone

    The second case is not a corner. `Message.sender` is NULL whenever somebody
    with no account texts into a group, which is the scenario S6 of the
    blocking spec uses to ILLUSTRATE the feature - and the spec's own
    pseudocode (`if sender and ...`) quietly does nothing there.

    TWO ANSWERS, ONE QUERY. A recipient may be an account or may be only an
    address, so the screen carries both sets - see `Screen`. They come from the
    same rows, so asking for the second costs nothing.
    """
    condition = Q()
    matched = False

    if user_id is not None:
        condition |= Q(blocker_id=user_id) | Q(blocked_user_id=user_id)
        matched = True

    number = _normalised(phone_number)
    if number is not None:
        condition |= Q(blocked_phone_number=number)
        matched = True

    address = _normalised_email(email)
    if address is not None:
        condition |= Q(blocked_email=address)
        matched = True

    if not matched:
        return Screen()

    rows = UserBlock.objects.filter(condition).values_list(
        'blocker_id', 'blocked_user_id', 'blocked_email'
    )

    screened = set()
    addresses = set()
    for blocker_id, blocked_id, blocked_email in rows:
        screened.add(blocker_id)
        if blocked_id is not None:
            screened.add(blocked_id)
        # Only the sender's OWN blocks name an address the fan-out can screen.
        # A row where somebody else blocked this address says nothing about
        # whether THIS sender may reach it.
        if blocked_email and blocker_id == user_id:
            addresses.add(blocked_email)
    screened.discard(user_id)
    return Screen(users=frozenset(screened), emails=frozenset(addresses))


def blocked_user_ids(viewer):
    """Every account id in a block relationship with `viewer`, either way.

    For the queryset that hides them from search. Returns ids rather than a
    queryset so the caller can use it in an `__in` without a correlated
    subquery - a person blocks a handful of people, not thousands.
    """
    if viewer is None or not viewer.is_authenticated:
        return []

    rows = UserBlock.objects.filter(
        Q(blocker=viewer) | Q(blocked_user=viewer)
    ).values_list('blocker_id', 'blocked_user_id')

    ids = set()
    for blocker_id, blocked_id in rows:
        ids.add(blocker_id)
        if blocked_id is not None:
            ids.add(blocked_id)
    ids.discard(viewer.pk)
    return list(ids)


def _one_target(user, phone_number, email=None):
    named = [target for target in (user, phone_number, email) if target]
    if len(named) != 1:
        raise ValueError(
            'Name the other party by exactly one of user, phone_number or email.'
        )


def _normalised(phone_number):
    """Numbers are stored in one canonical form, so a lookup is exact."""
    return (phone_number or '').strip() or None


def _normalised_email(email):
    """Addresses are compared lowercased, the way `invite_by_email` stores them.

    Nobody thinks of Stewart@x.com and stewart@x.com as two people, and a block
    that could be walked around by changing the case would not be a block.
    """
    return (email or '').strip().lower() or None
