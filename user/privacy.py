"""The Privacy settings, enforced.

One module, the way `user/blocks.py` is one module, because the same four
questions are asked from several places and three copies of an answer is three
chances to disagree:

    may this person be direct-messaged by that one   `allows_direct_message`
    may they be added to a group by that one         `allows_group_add`
    can they be found by number or address           `discoverable_by`
    are their read receipts shown                    `shows_read_receipts`

The screen that sets all of it is Settings → Privacy
(`user/models/privacy_settings.py`).

A MISSING ROW IS THE DEFAULTS, AND THE DEFAULTS ARE OPEN. Rows are made at
sign-in, so a missing one means somebody who has never changed a setting -
`UserSettings.of` reads without writing, because a member list or a fan-out
must not insert a row per person it merely asked about.

NOT BLOCKING, AND ASKED AFTER IT. A block is a fact about a PAIR and always
wins; these are one person's standing answer to everybody. The refusals here
also read differently on purpose: a block's wording reveals nothing, because
saying "you are blocked" is itself the leak, while a privacy setting is not a
secret - "this person doesn't accept direct messages" is what the sender needs
to know, and Yul chose that wording (2026-09-16).

"PEOPLE IN MY LOOPS" IS THE ONE RELATIONSHIP THERE IS. The product has no
contacts list, so the middle option of each audience means "we are both active
members of at least one group" - `shares_a_loop`.
"""

#: What somebody is told when a privacy setting refuses their direct message.
DIRECT_MESSAGES_REFUSED = "This person doesn't accept direct messages."


def shares_a_loop(one, other):
    """Whether these two are both active members of at least one GROUP.

    Groups only: two people in a direct conversation are not "in a Loop
    together", and counting it would make the middle option mean "anybody who
    has ever messaged me", which is the opposite of what it offers.
    """
    from chat.models import Conversation, Participant

    if one is None or other is None:
        return False

    return Participant.objects.filter(
        user=one,
        status=Participant.Status.ACTIVE,
        conversation__kind=Conversation.Kind.GROUP,
        conversation__participants__user=other,
        conversation__participants__status=Participant.Status.ACTIVE,
    ).exists()


def _audience_allows(answer, person, other):
    from user.models import PrivacySettings

    if answer == PrivacySettings.Audience.NOBODY:
        return False
    if answer == PrivacySettings.Audience.LOOPS:
        return shares_a_loop(person, other)
    return True


def allows_direct_message(recipient, sender):
    """Whether `sender` may START a direct conversation with `recipient`.

    Only starting one. A conversation the two already have carries on - the way
    out of that is blocking, not a setting that would strand a thread somebody
    is already in.
    """
    from user.models import PrivacySettings

    if recipient is None or sender is None or recipient.pk == sender.pk:
        return True
    return _audience_allows(
        PrivacySettings.of(recipient).direct_messages_from, recipient, sender
    )


def allows_group_add(person, adder):
    """Whether `adder` may put `person` into a group.

    `adder` is None for anything the system did on its own - claiming an
    invitation nobody can be named for - and that is allowed: this answers
    "who may add me", and nobody is not somebody.

    Joining by link, code or a join request is the person's own doing and never
    asks this.
    """
    from user.models import PrivacySettings

    if person is None or adder is None or person.pk == adder.pk:
        return True
    return _audience_allows(
        PrivacySettings.of(person).group_adds_from, person, adder
    )


def group_add_refusal(users):
    """What an admin is told when the people they named do not allow it.

    Names them, because the add is all-or-nothing: an admin who is told only
    "somebody refused" cannot finish the job by removing that person from the
    list.
    """
    names = ', '.join(f'@{user.username}' for user in users)
    if len(users) == 1:
        return f"{names} doesn't accept being added to groups."
    return f"These people don't accept being added to groups: {names}."


def discoverable_by(field):
    """A filter for `find_user` and `search_users`: skip people who hid this.

    `field` is `discoverable_by_phone` or `discoverable_by_email`. A NULL row
    has to pass as well as a True column: a missing row is the defaults, and
    the default is discoverable.
    """
    from django.db.models import Q

    return Q(**{f'privacy_settings__{field}': True}) | Q(
        privacy_settings__isnull=True
    )


def shows_read_receipts(user):
    """Whether this person's read positions may be shown to other people.

    BOTH WAYS (Yul's call, 2026-09-16, the bargain WhatsApp makes): with it off
    their reads are hidden from everybody, and everybody else's are hidden from
    them. One-way would let somebody take the receipts without giving any.

    Their own reads are always visible to themselves - that is what clears
    their own badge, and it is not a receipt.
    """
    from user.models import PrivacySettings

    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    return PrivacySettings.of(user).read_receipts_enabled
