"""The notification app's models, one file per model.

Everything is re-exported here, so `from notification.models import
Notification` works whichever file a model lives in.

Two models, and they answer different halves of the same question. Notification
is WHAT happened to somebody, kept so it survives them not looking.
PushSubscription is WHERE to reach them when the app is closed.
"""
from .notification import Notification
from .push_subscription import PushSubscription

__all__ = [
    'Notification',
    'PushSubscription',
]
