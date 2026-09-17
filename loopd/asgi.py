"""ASGI entrypoint: HTTP through Django, websockets through Channels.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""
import os

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import OriginValidator
from django.conf import settings
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'loopd.settings')

# Must be built before importing anything that touches models, so the app
# registry is populated first.
django_asgi_application = get_asgi_application()

from chat.routing import websocket_urlpatterns  # noqa: E402
from user.channels_auth import JWTAuthMiddleware  # noqa: E402

application = ProtocolTypeRouter(
    {
        'http': django_asgi_application,
        # Origin validation against the same list CORS uses. A websocket
        # handshake and an XHR ask the same question - "may this browser origin
        # talk to the API" - while ALLOWED_HOSTS answers a different one: which
        # Host headers this server responds to, which only ever names the API
        # itself. AllowedHostsOriginValidator compares the Origin's HOSTNAME
        # against those, so a frontend on any other host was refused - which is
        # why sockets failed from the web app while REST from the same page
        # worked.
        #
        # Some validator is required: without one, any website could open a
        # socket as a logged-in visitor. An empty list refuses every socket,
        # which is the correct default.
        #
        # Note the matching gets STRICTER, not just different. A pattern with a
        # scheme (as every CORS origin has) is matched on scheme, host AND
        # port; ALLOWED_HOSTS entries have no scheme, so they only ever matched
        # on host. An http:// origin will no longer satisfy an https:// entry.
        'websocket': OriginValidator(
            JWTAuthMiddleware(URLRouter(websocket_urlpatterns)),
            settings.CORS_ALLOWED_ORIGINS,
        ),
    }
)
