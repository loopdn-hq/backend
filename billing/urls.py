"""Routes for the billing app, mounted at /api/v1/billing/ by loopd.urls."""
from django.urls import path

from billing.viewsets.v1.billing import (
    BillingPortalSessionView,
    BillingSummaryView,
    CreditCheckoutSessionView,
    CreditPackageListView,
    PlanCheckoutSessionView,
    PlanListView,
)
from billing.webhooks import StripeWebhook

app_name = 'billing'

urlpatterns = [
    path('webhooks/stripe/', StripeWebhook.as_view(), name='stripe-webhook'),

    path('plans/', PlanListView.as_view(), name='plans'),
    path('credit-packages/', CreditPackageListView.as_view(), name='credit-packages'),
    path('summary/', BillingSummaryView.as_view(), name='summary'),
    path('checkout/plan/', PlanCheckoutSessionView.as_view(), name='checkout-plan'),
    path('checkout/credits/', CreditCheckoutSessionView.as_view(), name='checkout-credits'),
    path('portal/', BillingPortalSessionView.as_view(), name='portal'),
]
