"""Routes for the chat app, mounted at /api/v1/chat/ by loopd.urls."""
from rest_framework.routers import SimpleRouter

from chat.viewsets.v1.conversations import ConversationViewSet

app_name = 'chat'

router = SimpleRouter()
router.register('conversations', ConversationViewSet, basename='conversation')

urlpatterns = router.urls
