"""The page that stops a group emailing somebody.

The third route in this project that something other than our own frontend
calls, and the only one a PERSON reaches directly. It is served by Django and
not by the frontend on purpose: `frontend-web` is a static export on another
origin, so it could not verify a token, would have to POST back through CORS -
which per CLAUDE.md also gates websockets and is empty under DEBUG=False - and
would fail inside a mail client's restricted webview. All of that in a page
whose entire job is one button, and whose whole point is that it must never be
the thing that does not work.

GET IS INERT. THAT IS THE WHOLE DESIGN.
---------------------------------------
Corporate mail security - Proofpoint, Mimecast, Defender Safe Links - fetches
every URL in every inbound message before the recipient sees it. A GET that
unsubscribed would silently opt out an entire company the moment the first
email landed. So GET renders a confirmation and writes nothing, and POST is
what acts. That is exactly why RFC 8058 mandates POST for one-click, and the
same endpoint serves both: a mail client's automated POST and a human pressing
the button are the same request.

ALWAYS 200
----------
Valid token, tampered token, unknown membership, somebody who left, already
declined, over the rate limit: one status, one page. A one-click sender reads
the status and nothing else, so distinguishing them buys nothing and would turn
the endpoint into an oracle for which membership ids exist.

The one thing that does vary is that a verified token's page NAMES the group -
which discloses nothing, because whoever holds the token holds the email, and
that email already carries the group name in its From and its Subject. Naming
it is what makes the scope obvious at the moment the decision is made.
"""
import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer, TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from chat import membership
from chat.models import Participant
from mail.tokens import participant_id_from_unsubscribe

logger = logging.getLogger(__name__)


class UnsubscribeView(APIView):
    """Confirm, then stop, one group's email for one member."""

    # No authentication class at all, deliberately. DRF enforces CSRF only
    # inside SessionAuthentication, and an RFC 8058 POST arrives with no
    # cookie, no token and no referer - anything that reintroduced CSRF
    # enforcement here would break one-click silently, which is the failure
    # mode nobody notices.
    authentication_classes = []
    permission_classes = [AllowAny]
    # HTML for the browser, JSON for the mail client that POSTs and reads a
    # status. The envelope renderer is for clients that expect
    # `{code, message, data}` and neither of these is one - the same reasoning
    # mail/webhooks.py gives.
    renderer_classes = [TemplateHTMLRenderer, JSONRenderer]
    template_name = 'mail/unsubscribe.html'

    def get(self, request, token):
        """Show what pressing the button will do. Writes nothing."""
        participant = _resolve(token)
        return self._page(
            request,
            template='mail/unsubscribe.html',
            participant=participant,
            token=token,
        )

    def post(self, request, token):
        """Stop this group's email for this member. Idempotent."""
        participant = _resolve(token)

        if participant is not None and _within_attempt_cap(token):
            changed = membership.decline_email(participant)
            if changed:
                logger.info(
                    'Participant %s stopped email for conversation %s',
                    participant.pk, participant.conversation_id,
                )

        return self._page(
            request,
            template='mail/unsubscribed.html',
            participant=participant,
            token=token,
        )

    def _page(self, request, *, template, participant, token):
        """One response shape for every outcome. See the module docstring."""
        group_name = participant.conversation.name if participant else ''
        response = Response(
            {
                'ok': True,
                'group_name': group_name,
                'token': token,
                'app_url': settings.FRONTEND_URL,
                'product_name': settings.EMAIL_PRODUCT_NAME,
                'support_address': settings.EMAIL_SUPPORT_ADDRESS,
            },
            status=status.HTTP_200_OK,
            template_name=template,
        )
        # Nothing about this page may be cached or indexed: the URL is a
        # credential, and a shared cache in front of it would serve one
        # member's page to the next person who asked.
        response['Cache-Control'] = 'no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


def _resolve(token):
    """The membership an unsubscribe token names, or None.

    Deliberately does not filter by status. The link outlives the membership -
    see `membership.decline_email` - so somebody who has left still gets a page
    that works and an answer that is recorded.
    """
    participant_id = participant_id_from_unsubscribe(token)
    if participant_id is None:
        return None

    return (
        Participant.objects.filter(pk=participant_id)
        .select_related('conversation')
        .first()
    )


def _within_attempt_cap(token):
    """Whether this token may act again this hour.

    The house fixed-window counter, hashed for the reason otp/services.py
    gives: cache keys surface in logs and memory dumps, and this one is a
    credential.

    Being refused is invisible on purpose. The operation is idempotent, so a
    caller past the cap is shown the same "done" page - which is true, because
    twenty POSTs in an hour means the state is already declined.
    """
    target = hashlib.sha256((token or '').encode()).hexdigest()[:32]
    key = f'mail:unsub:h:{target}'
    if (cache.get(key) or 0) >= settings.MAIL_MAX_UNSUBSCRIBE_ATTEMPTS_PER_HOUR:
        return False
    cache.add(key, 0, 3600)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, 3600)
    return True
