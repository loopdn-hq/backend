"""ASGI entrypoint: HTTP through Django, websockets through Channels.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""
import os

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
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
        # AllowedHostsOriginValidator rejects cross-origin handshakes using
        # ALLOWED_HOSTS - without it any website could open a socket as a
        # logged-in visitor.
        'websocket': AllowedHostsOriginValidator(
            JWTAuthMiddleware(URLRouter(websocket_urlpatterns))
        ),
    }
)
