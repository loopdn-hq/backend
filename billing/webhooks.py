"""Receives events Stripe pushes about payments and subscriptions.

Like sms/webhooks.py, this is an unauthenticated public endpoint - the caller
is Stripe's servers, not a person with a token, and the only proof of that is
a signature over the raw body. Unlike the SMS provider, Stripe's own SDK does
the signature verification (`stripe.Webhook.construct_event`), so there is no
hand-rolled crypto here.

STATUS CODES ARE THE CONTRACT
-----------------------------
Stripe reads any non-2xx as "that did not arrive" and retries for hours:

  200  every outcome we understand and will not change our minds about -
       duplicate event, or an event type we do not act on.
  400  a bad signature, or an unparseable payload. Never retried.
  5xx  we failed to process a well-formed, known event. The de-duplication
       row is removed first, so the retry can reprocess it rather than being
       waved through as a duplicate.

Plans and credit packages are matched back from a Stripe object by price id
(`services.plan_code_for_stripe_price`), so the mapping lives in one place -
the Plan/CreditPackage rows themselves, kept in sync with Stripe by
`manage.py sync_stripe_billing` - and never needs updating here when a price
changes.
"""
import logging

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import services
from billing.models import StripeWebhookEvent, Subscription

logger = logging.getLogger(__name__)


class StripeWebhook(APIView):
    """Receives everything Stripe has to tell us about an event."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # Stripe expects a bare status code and reads no body, so this answers in
    # plain JSON rather than the project's `{code, message, data}` envelope.
    renderer_classes = [JSONRenderer]

    def post(self, request):
        try:
            event = stripe.Webhook.construct_event(
                request.body,
                request.META.get('HTTP_STRIPE_SIGNATURE', ''),
                settings.STRIPE_WEBHOOK_SECRET,
            )
        except ValueError:
            logger.warning('Unparseable Stripe webhook body')
            return Response({'ok': False}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.SignatureVerificationError:
            logger.warning('Stripe webhook signature verification failed')
            return Response({'ok': False}, status=status.HTTP_400_BAD_REQUEST)

        event_id = event['id']
        event_type = event['type']

        # De-duplicate FIRST, before anything with a side effect. A retried
        # event that got as far as granting access must not grant it twice.
        _, created = StripeWebhookEvent.objects.get_or_create(
            event_id=event_id, defaults={'event_type': event_type}
        )
        if not created:
            return Response({'ok': True, 'duplicate': True}, status=status.HTTP_200_OK)

        try:
            # `event['data']['object']` is a Stripe SDK object (Session,
            # Subscription, ...) coming from the real `construct_event`, not a
            # dict - newer stripe-python versions raise AttributeError on
            # `.get(...)` against one rather than falling through to dict
            # behaviour. `_handle` and its helpers are all written against a
            # plain dict, so convert once here rather than bracket-accessing
            # everything downstream. Tests that stub `construct_event` hand
            # back an already-plain dict, which has no `.to_dict()` - kept as
            # is in that case.
            data_object = event['data']['object']
            if hasattr(data_object, 'to_dict'):
                data_object = data_object.to_dict()
            self._handle(event_type, data_object)
        except Exception:
            # Delete the dedupe row so Stripe's retry can actually reprocess
            # this event rather than being waved through as a duplicate.
            StripeWebhookEvent.objects.filter(event_id=event_id).delete()
            logger.exception('Failed handling Stripe event %s; asking for a retry',
                             event_id)
            return Response(
                {'ok': False}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({'ok': True}, status=status.HTTP_200_OK)

    def _handle(self, event_type, data_object):
        if event_type == 'checkout.session.completed':
            self._handle_checkout_completed(data_object)
        elif event_type in ('customer.subscription.updated', 'customer.subscription.created'):
            self._handle_subscription_updated(data_object)
        elif event_type == 'customer.subscription.deleted':
            self._handle_subscription_deleted(data_object)
        else:
            logger.info('Received Stripe event %s (unhandled)', event_type)

    def _user_for(self, *, client_reference_id=None, metadata=None, customer_id=None):
        """Every event handled here carries one of these ways back to a user."""
        user_model = get_user_model()
        user_id = client_reference_id or (metadata or {}).get('user_id')
        if user_id:
            return user_model.objects.filter(pk=user_id).first()
        if customer_id:
            subscription = Subscription.objects.filter(stripe_customer_id=customer_id).first()
            return subscription.user if subscription else None
        return None

    def _handle_checkout_completed(self, session):
        user = self._user_for(
            client_reference_id=session.get('client_reference_id'),
            metadata=session.get('metadata'),
            customer_id=session.get('customer'),
        )
        if user is None:
            logger.error('Stripe checkout.session.completed with no resolvable user: %s',
                         session.get('id'))
            return

        if session.get('mode') == 'subscription':
            # Retrieve returns another Stripe SDK object, not a dict - same
            # conversion as the top-level event, for the same reason.
            stripe_subscription = stripe.Subscription.retrieve(session['subscription']).to_dict()
            self._sync_subscription(user, stripe_subscription)
        elif session.get('mode') == 'payment':
            package_id = (session.get('metadata') or {}).get('credit_package_id')
            if not package_id:
                logger.error('Stripe payment checkout with no credit_package_id: %s',
                             session.get('id'))
                return
            from billing.models import CreditPackage
            package = CreditPackage.objects.filter(pk=package_id).first()
            if package is None:
                logger.error('Unknown credit package %s in checkout %s',
                             package_id, session.get('id'))
                return
            services.credit_wallet(
                user,
                amount_cents=package.credit_cents,
                kind='credit_purchase',
                description=f'Purchased {package.name}',
                stripe_payment_intent_id=session.get('payment_intent') or '',
            )

    def _handle_subscription_updated(self, stripe_subscription):
        user = self._user_for(customer_id=stripe_subscription.get('customer'))
        if user is None:
            logger.error('Stripe subscription event with no resolvable user: %s',
                         stripe_subscription.get('id'))
            return
        self._sync_subscription(user, stripe_subscription)

    def _handle_subscription_deleted(self, stripe_subscription):
        user = self._user_for(customer_id=stripe_subscription.get('customer'))
        if user is None:
            logger.error('Stripe subscription.deleted with no resolvable user: %s',
                         stripe_subscription.get('id'))
            return
        services.downgrade_to_free(user)

    def _sync_subscription(self, user, stripe_subscription):
        price_id = stripe_subscription['items']['data'][0]['price']['id']
        plan_code = services.plan_code_for_stripe_price(price_id)
        if plan_code is None:
            logger.error('Stripe price %s does not match any Plan', price_id)
            return

        services.activate_subscription(
            user,
            plan_code=plan_code,
            stripe_subscription_id=stripe_subscription['id'],
            status=stripe_subscription['status'],
            current_period_end=services.stripe_timestamp_to_datetime(
                stripe_subscription.get('current_period_end')
            ),
            cancel_at_period_end=stripe_subscription.get('cancel_at_period_end', False),
        )
