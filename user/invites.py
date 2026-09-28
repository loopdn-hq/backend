"""Inviting somebody who has no account yet.

A direct conversation is a pair of accounts - `chat.Conversation.direct_key` is
two user ids - so typing the address of somebody who is not here can only ever
end in "nobody found". This is what the New conversation box offers instead.

THIS MUST NOT BECOME A WAY TO ASK WHETHER AN ADDRESS HAS AN ACCOUNT. An address
whose owner turned "Find me using my email" off, and one belonging to somebody
on either side of a block with the inviter, are answered exactly like an address
nobody has - and nothing is sent to either. Any other answer would make this
endpoint the lookup that Settings → Privacy and `UserBlock` refuse, which is
also why the cap below counts attempts rather than sends.

EMAIL ONLY. Texting an invitation needs a number to send it from, and every
number the product owns belongs to one Loop (`sms.GroupNumber`), with that
Loop's consent and STOP handling around it. Telnyx's Verify API can text a
stranger, but only a code of its own - never a link. So the web app hands the
person a link to send themselves instead; see the frontend's
NewConversationModal.
"""
import logging
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache

from user.lookups import find_user
from utils.mail import APP_INVITATION, send_templated_email

logger = logging.getLogger(__name__)

#: Per inviter, per hour. An invitation is mail to somebody who never asked for
#: it, so the cap is low: inviting an address book in an hour is the shape of
#: abuse, not of use.
INVITES_PER_HOUR = 20

#: An invitation went out - or was silently declined. The caller cannot tell
#: which, and that is the point; see the module docstring.
SENT = 'sent'

#: The address belongs to an account the inviter can already see, so there is
#: nobody to invite. This says no more than opening the conversation would.
ALREADY_HERE = 'already_here'


class TooManyInvites(Exception):
    """This inviter has used up `INVITES_PER_HOUR`."""


def invite_to_app(inviter, email):
    """Email an invitation to Loopd'n. Returns `SENT` or `ALREADY_HERE`."""
    address = (email or '').strip().lower()

    if not consume_invite_allowance(inviter):
        raise TooManyInvites

    # The inviter's own view of the world, blocks and discoverability included:
    # anybody this returns is somebody they could simply have messaged.
    if find_user(address, exclude=inviter) is not None:
        return ALREADY_HERE

    # Nobody that lookup would return - but the address may still be on an
    # account that hid it, one in a block relationship with the inviter, or the
    # inviter's own. Mailing any of them "come and join" would say that the
    # address is taken and that its owner is here, so nothing is sent and the
    # answer is the one a stranger's address gets.
    if find_user(address, respect_discoverability=False) is not None:
        logger.info('Invitation not sent: the address is already on an account')
        return SENT

    name = getattr(getattr(inviter, 'profile', None), 'display_label', None)
    send_templated_email(
        APP_INVITATION,
        to=address,
        context={
            'inviter_name': name or 'Somebody',
            # Deliberately not a token, the same as a group invitation: signing
            # up and confirming this address is what makes them reachable, so a
            # forwarded invitation gives the reader nothing.
            'signup_url': f'{settings.FRONTEND_URL}/signup?email={quote(address)}',
        },
    )
    return SENT


def consume_invite_allowance(inviter):
    """Whether this person may reach one more stranger, counting every ATTEMPT.

    Attempts rather than sends, because a silently declined invitation costs
    the sender nothing to repeat: counting only what goes out would leave the
    probe that the silence exists to stop running at full speed.

    SHARED WITH THE EXTERNAL CONVERSATION PATH, and deliberately one bucket
    rather than two. `chat/serializers.py:DirectConversationSerializer` opens a
    thread with an address that has no account, which puts an email in front of
    a stranger exactly as an invitation does - and two caps of twenty is a cap
    of forty for anybody willing to use both doors.
    """
    key = f'user:invites:{inviter.pk}'
    if (cache.get(key) or 0) >= INVITES_PER_HOUR:
        return False

    cache.add(key, 0, 3600)
    try:
        cache.incr(key)
    except ValueError:
        # The key expired between the add and the incr. Starting again at one
        # loses at most one invitation's worth of counting.
        cache.set(key, 1, 3600)
    return True
