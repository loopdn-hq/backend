"""Finding one user from something a person typed.

Chat needs this in three places - starting a direct conversation, adding
someone to a group, and the search box - so the rules live in one place.

Only active users are ever returned: an account that has not verified its
email must not be discoverable, let alone messageable.

AND NEITHER PARTY OF A BLOCK IS RETURNED TO THE OTHER, whenever a viewer is
given. Both functions take a viewer as `exclude` - it already means "not me" -
and the block filter rides alongside it, so somebody who has been blocked
simply is not there, which is the same answer an unused username gives. Both
functions, deliberately: hiding somebody from `search_users` while `find_user`
still resolves their exact username would make the rule cosmetic.

The `exclude=None` case is untouched, and one caller relies on that -
`chat/membership.py:invite_by_email` passes no viewer. That is the right way
round: a block must never make `find_user` return None THERE, because the
caller reads None as "nobody has this address" and emails them an invitation.
Refusing an invitation is the invite path's own job, not this function's.

`apply_blocks=False` is the second exception, and there is exactly one caller.
Starting a direct conversation has to tell the two sides of a block APART - the
blocker is told plainly, the blocked person is told nothing - and it cannot do
that if the person has already been filtered out of existence. It re-resolves
with the filter off only after the ordinary lookup has already missed, so the
default stays safe and the slow path costs one extra query on a miss.
"""
from django.contrib.auth.models import User

from user import privacy
from user.blocks import blocked_user_ids

# Below this, a search is a directory dump rather than a lookup.
MIN_SEARCH_LENGTH = 2


def active_users():
    return User.objects.select_related('profile').filter(is_active=True)


def find_user(identifier, exclude=None, *, apply_blocks=True,
              respect_discoverability=True):
    """Resolve a username, an email address or a phone number to one user.

    Returns None if nothing matches.

    THE NUMBER AND ADDRESS BRANCHES OBEY Settings → Privacy. Somebody who
    turned "Find me using my phone number" or "…my email" off is not returned
    by that branch, here or in `search_users` - so they cannot be direct-
    messaged, added or invited by that identifier. Their @username still finds
    them: these two switches are about the identifiers people hand out, not a
    way to disappear.

    `respect_discoverability=False` has exactly one caller, and it is not a
    hole: `chat/membership.py:invite_by_email` asks a second time, after the
    ordinary lookup has already missed, only to find out whether the address
    belongs to a hidden account - so it can decline to email an invitation
    that could never be claimed. It never returns that user to anybody.

    A leading '+' means the input is a phone number - the only form profiles
    store - so there is never any ambiguity with a numeric username.

    An email only matches when its owner has **verified** it, for exactly the
    reason the phone rule exists. `email` is not unique and nothing stops an
    account PATCHing its address to one it does not own: that clears
    `email_verified` but leaves `is_active` True and existing tokens valid, so
    without this filter the squatter is who a lookup returns - and typing the
    real owner's address into a group would put the squatter inside it.
    `is_active` is NOT that gate; it means an admin has not banned the account.

    An '@' means an email address is worth trying, and it is only ever a TRY:
    the username branch still runs afterwards. Django allows '@' in a username,
    so treating the character as proof would make such an account unaddressable
    the moment email lookup was added.

    A number only matches when its owner has **verified** it. `phone_number` is
    unique independently of `phone_no_verified`, so anybody can type somebody
    else's number into their own profile and hold it there unproven. Without
    this filter, adding that number to a group would resolve to the squatter
    and put them inside a private conversation meant for its real owner - and
    once a group's messages are also delivered by SMS, to their handset too.
    """
    identifier = (identifier or '').strip()
    if not identifier:
        return None

    queryset = active_users()
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)
        if apply_blocks:
            queryset = queryset.exclude(pk__in=blocked_user_ids(exclude))

    if identifier.startswith('+'):
        by_number = queryset.filter(
            profile__phone_number=identifier, profile__phone_no_verified=True
        )
        if respect_discoverability:
            by_number = by_number.filter(privacy.discoverable_by('discoverable_by_phone'))
        return by_number.first()

    if '@' in identifier:
        by_email = queryset.filter(
            email__iexact=identifier, profile__email_verified=True
        )
        if respect_discoverability:
            by_email = by_email.filter(privacy.discoverable_by('discoverable_by_email'))
        found = by_email.first()
        if found is not None:
            return found

    return queryset.filter(username__iexact=identifier).first()


def search_users(term, exclude=None):
    """Users matching a partial username, or an exact phone number.

    Phone matching is exact on purpose. A prefix search over phone numbers
    would let anyone walk the entire user directory a digit at a time; you
    can only find someone by number if you already know their number.

    It also requires the number to be verified, for the same reason `find_user`
    does - and the reason is stronger here, not weaker. Search returning an
    unverified claim is not merely a wrong answer: the client shows that person,
    the user taps them, and the client then starts a conversation by *username*,
    which never passes back through the number check at all. Leaving this
    unfiltered would route around `find_user` entirely.
    """
    term = (term or '').strip()
    if len(term) < MIN_SEARCH_LENGTH:
        return active_users().none()

    queryset = active_users()
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)
        queryset = queryset.exclude(pk__in=blocked_user_ids(exclude))

    if term.startswith('+'):
        # And "Find me using my phone number", for the same reason `find_user`
        # obeys it: hiding somebody there while search still returned them
        # would make the switch cosmetic.
        return (
            queryset.filter(
                profile__phone_number=term, profile__phone_no_verified=True
            )
            .filter(privacy.discoverable_by('discoverable_by_phone'))
            .order_by('username')
        )

    return queryset.filter(username__istartswith=term).order_by('username')
