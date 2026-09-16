"""Routes for the push app, mounted at /api/v1/push/ by loopd.urls.

Plain paths rather than a router: these are three actions on the caller's own
browser, not a collection anybody lists or reads back. No id appears in any of
them, so there is no route here that can reach somebody else's subscription.
"""
from django.urls import path

from notification.viewsets.v1.push import (
    PushKeyView,
    PushSubscriptionRemoveView,
    PushSubscriptionView,
)

app_name = 'push'

urlpatterns = [
    path('key/', PushKeyView.as_view(), name='key'),
    path('subscriptions/', PushSubscriptionView.as_view(), name='subscribe'),
    path(
        'subscriptions/remove/',
        PushSubscriptionRemoveView.as_view(),
        name='unsubscribe',
    ),
]
