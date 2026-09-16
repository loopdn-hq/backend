"""Routes for the enterprise app, mounted at /api/v1/enterprise/ by loopd.urls.

No webhook route here - Stripe events for Enterprise contracts are handled
by billing.webhooks.StripeWebhook, the project's one Stripe webhook endpoint.
"""
from django.urls import path

from enterprise.viewsets.v1.enterprise import (
    BillingSummaryView,
    CreditsView,
    DraftAttachView,
    DraftCheckoutView,
    DraftDetailView,
    DraftListCreateView,
    PricingView,
)

app_name = 'enterprise'

urlpatterns = [
    path('pricing/', PricingView.as_view(), name='pricing'),

    path('drafts/', DraftListCreateView.as_view(), name='drafts'),
    path('drafts/<str:draft_id>/', DraftDetailView.as_view(), name='draft-detail'),
    path('drafts/<str:draft_id>/attach/', DraftAttachView.as_view(), name='draft-attach'),
    path('drafts/<str:draft_id>/checkout/', DraftCheckoutView.as_view(), name='draft-checkout'),

    path('billing/', BillingSummaryView.as_view(), name='billing'),
    path('credits/', CreditsView.as_view(), name='credits'),
]
