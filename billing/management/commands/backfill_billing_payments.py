"""Fill the transaction history with what Stripe was paid before the Payment
table existed. From then on the webhook records each payment as it happens.

Reads every paid invoice of every customer we know (plan and Enterprise
payments), and the PaymentIntent behind every credit top-up in the wallet
ledger. Idempotent: a payment already recorded is left as it is, so it is
safe to rerun, and safe to run while the webhook is live.

    python manage.py backfill_billing_payments
"""
import stripe
from django.core.management.base import BaseCommand

from billing import services
from billing.models import Payment, Subscription, WalletTransaction


class Command(BaseCommand):
    help = 'Record past Stripe payments (plans, Enterprise, credit top-ups) as Payment rows.'

    def handle(self, *args, **options):
        before = Payment.objects.count()
        self._backfill_invoices()
        self._backfill_credit_purchases()
        self.stdout.write(self.style.SUCCESS(
            f'Backfill complete. Payments recorded: {Payment.objects.count() - before}.'
        ))

    def _backfill_invoices(self):
        customer_ids = (
            Subscription.objects.exclude(stripe_customer_id='')
            .values_list('stripe_customer_id', flat=True).distinct()
        )
        for customer_id in customer_ids:
            try:
                invoices = stripe.Invoice.list(customer=customer_id, status='paid', limit=100)
                for invoice in invoices.auto_paging_iter():
                    services.record_invoice_payment(
                        invoice.to_dict() if hasattr(invoice, 'to_dict') else invoice
                    )
            except stripe.StripeError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Skipping customer {customer_id}: {exc}'
                ))

    def _backfill_credit_purchases(self):
        recorded = Payment.objects.values_list('stripe_reference', flat=True)
        purchases = (
            WalletTransaction.objects
            .filter(kind=WalletTransaction.Kind.CREDIT_PURCHASE)
            .exclude(stripe_payment_intent_id='')
            .exclude(stripe_payment_intent_id__in=recorded)
            .select_related('wallet__user')
        )
        for purchase in purchases:
            try:
                intent = stripe.PaymentIntent.retrieve(
                    purchase.stripe_payment_intent_id, expand=['latest_charge'],
                )
            except stripe.StripeError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Skipping top-up {purchase.stripe_payment_intent_id}: {exc}'
                ))
                continue
            if hasattr(intent, 'to_dict'):
                intent = intent.to_dict()

            latest_charge = intent.get('latest_charge')
            services.record_payment(
                purchase.wallet.user,
                kind=Payment.Kind.CREDIT_PURCHASE,
                # What was charged, not `purchase.amount_cents` - that is the
                # credit granted, which a promotional package makes larger.
                amount_cents=intent.get('amount_received') or intent.get('amount') or 0,
                currency=intent.get('currency'),
                description=purchase.description,
                stripe_reference=purchase.stripe_payment_intent_id,
                receipt_url=(
                    latest_charge.get('receipt_url') or ''
                    if isinstance(latest_charge, dict) else ''
                ),
                paid_at=purchase.created_at,
            )
