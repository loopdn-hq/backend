"""Finding a user's role on each axis.

One resolver per axis, and the axis is chosen by the SUBJECT passed to `can()`:
no subject means the account axis, a Conversation means the conversation axis.
Adding the advertiser axis later is one function and one entry in `RESOLVERS` -
which is the return on separating the axes at all.

Every resolver returns a SET of role names, not one role. The account axis is
additive (a user holds `standard` and `commercial` at once), and returning a set
from all of them keeps the resolver from caring which axis is which.
"""
from rbac.models import UserRole


def account_roles(user):
    """Every account role this user holds.

    Empty for an anonymous or unsaved user, which denies everything - the right
    answer, and the reason `standard` is a stored row rather than an implied
    default. A user who somehow has no rows is a bug we can see, not a
    permission level we invented.
    """
    if user is None or not getattr(user, 'pk', None) or not user.is_authenticated:
        return set()
    return set(
        UserRole.objects.filter(user=user).values_list('role', flat=True)
    )


def conversation_roles(user, conversation):
    """This user's role in this conversation, as a set of nought or one.

    ACTIVE only, deliberately: `Conversation.active_participants` makes the same
    restriction, and a pending, departed, removed or blocked row is still a row.
    Reading the role off a non-active membership would let somebody who was
    removed keep the powers they had.

    A one-to-one has no admins - both sides are MEMBER - so this returns
    `{'member'}` there, and `rbac/policy.py` is what knows a DM needs different
    treatment.
    """
    from chat.models import Participant

    if user is None or not getattr(user, 'pk', None) or not user.is_authenticated:
        return set()
    if conversation is None:
        return set()

    role = (
        Participant.objects.filter(
            conversation=conversation,
            user=user,
            status=Participant.Status.ACTIVE,
        )
        .values_list('role', flat=True)
        .first()
    )
    return {role} if role else set()


def axis_for(subject):
    """Which axis a subject selects. None means the account axis."""
    from chat.models import Conversation

    if subject is None:
        return 'account'
    if isinstance(subject, Conversation):
        return 'conversation'
    raise TypeError(
        f'No rbac axis for {type(subject).__name__}. Add a resolver in '
        f'rbac/scopes.py rather than passing it through.'
    )


def roles_for(user, subject=None):
    """The roles this user holds on the axis that `subject` selects."""
    axis = axis_for(subject)
    if axis == 'account':
        return account_roles(user)
    return conversation_roles(user, subject)
