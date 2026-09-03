"""Finding one user from something a person typed.

Chat needs this in three places - starting a direct conversation, adding
someone to a group, and the search box - so the rules live in one place.

Only active users are ever returned: an account that has not verified its
email must not be discoverable, let alone messageable.
"""
from django.contrib.auth.models import User

# Below this, a search is a directory dump rather than a lookup.
MIN_SEARCH_LENGTH = 2


def active_users():
    return User.objects.select_related('profile').filter(is_active=True)


def find_user(identifier, exclude=None):
    """Resolve a username or a phone number to exactly one user, or None.

    A leading '+' means the input is a phone number - the only form profiles
    store - so there is never any ambiguity with a numeric username.

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

    if identifier.startswith('+'):
        return queryset.filter(
            profile__phone_number=identifier, profile__phone_no_verified=True
        ).first()

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

    if term.startswith('+'):
        return queryset.filter(
            profile__phone_number=term, profile__phone_no_verified=True
        ).order_by('username')

    return queryset.filter(username__istartswith=term).order_by('username')
