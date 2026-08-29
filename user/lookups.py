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
    """
    identifier = (identifier or '').strip()
    if not identifier:
        return None

    queryset = active_users()
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)

    if identifier.startswith('+'):
        return queryset.filter(profile__phone_number=identifier).first()

    return queryset.filter(username__iexact=identifier).first()


def search_users(term, exclude=None):
    """Users matching a partial username, or an exact phone number.

    Phone matching is exact on purpose. A prefix search over phone numbers
    would let anyone walk the entire user directory a digit at a time; you
    can only find someone by number if you already know their number.
    """
    term = (term or '').strip()
    if len(term) < MIN_SEARCH_LENGTH:
        return active_users().none()

    queryset = active_users()
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)

    if term.startswith('+'):
        return queryset.filter(profile__phone_number=term).order_by('username')

    return queryset.filter(username__istartswith=term).order_by('username')
