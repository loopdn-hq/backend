"""The notification app's models, one file per model.

Everything is re-exported here, so `from notification.models import
Notification` works whichever file a model lives in.
"""
from .notification import Notification

__all__ = [
    'Notification',
]
