"""Routes for the notification app, mounted at /api/v1/chat/notifications/ by
loopd.urls.

Still under chat/ in the URL, deliberately: it is the address the web app
calls, and moving the model out of the chat app was not a reason to move the
address. The prefix is empty because the mount point already names it.
"""
from rest_framework.routers import SimpleRouter

from notification.viewsets.v1.notifications import NotificationViewSet

app_name = 'notification'

router = SimpleRouter()
router.register('', NotificationViewSet, basename='notification')

urlpatterns = router.urls
