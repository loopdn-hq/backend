"""The one commercial address a member's browser opens directly: a sponsored
card's button, /c/{token}/ (slice 6).

Served by Django at the site root, not under /api/v1/ - it is a link, opened
in a new tab with no app token, and it must keep working as long as the card
is on somebody's screen - and with no "ads" in it, which ad blockers drop.
The rules are services/clicks.py's; this only reads the request and
redirects.

The redirect sends no referrer, is never cached, and adds nothing about the
viewer to the destination. A token that is not ours gets a plain 404, the
same for every kind of wrong.
"""
from django.http import HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_safe

from commercial.services import clicks


@require_safe
def follow_click(request, token):
    destination = clicks.follow(
        token,
        ip=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', ''),
        record=request.method == 'GET' and not _is_prefetch(request),
    )
    if destination is None:
        response = HttpResponse(
            "This link isn't valid.", status=404, content_type='text/plain; charset=utf-8'
        )
    else:
        response = HttpResponseRedirect(destination)
    response['Referrer-Policy'] = 'no-referrer'
    response['Cache-Control'] = 'no-store'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def _client_ip(request):
    """The first hop: behind the load balancer REMOTE_ADDR is the balancer.
    Only ever hashed, to tell repeats apart - spoofing it gains nothing."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _is_prefetch(request):
    purpose = (
        request.META.get('HTTP_SEC_PURPOSE', '') or request.META.get('HTTP_PURPOSE', '')
    ).lower()
    return 'prefetch' in purpose or 'prerender' in purpose
