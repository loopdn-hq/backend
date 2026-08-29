"""Websocket routes.

One socket per user, not one per conversation: a client opens `/ws/chat/`
once and receives events for every conversation it belongs to.
"""
from django.urls import path

from chat.consumers import ChatConsumer

websocket_urlpatterns = [
    path('ws/chat/', ChatConsumer.as_asgi()),
]
