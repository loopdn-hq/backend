"""Create/update the Stripe Products and Prices behind every Plan and
CreditPackage, and write the resulting price ids back onto those rows.

Idempotent: reruns update the existing Product/Price by the id already
stored rather than creating duplicates, so it is safe to call again after
editing a plan's price in the admin (Stripe prices are immutable - this
retires the old one and mints a replacement).

    python manage.py sync_stripe_billing
"""
import stripe
from django.core.management.base import BaseCommand

from billing.models import CreditPackage, Plan


class Command(BaseCommand):
    help = 'Create or update Stripe Products/Prices for every Plan and CreditPackage.'

    def handle(self, *args, **options):
        for plan in Plan.objects.exclude(code=Plan.Code.FREE).filter(is_custom_pricing=False):
            self._sync_plan(plan)
        for package in CreditPackage.objects.filter(active=True):
            self._sync_package(package)

    def _sync_plan(self, plan):
        if plan.monthly_price_cents is None:
            self.stdout.write(self.style.WARNING(
                f'Skipping {plan.code}: no monthly_price_cents set.'
            ))
            return

        product = stripe.Product.create(
            name=f'Loopd\u2019n {plan.name}',
            metadata={'plan_code': plan.code},
        )
        price = stripe.Price.create(
            product=product['id'],
            unit_amount=plan.monthly_price_cents,
            currency='usd',
            recurring={'interval': 'month'},
            metadata={'plan_code': plan.code},
        )
        plan.stripe_price_id = price['id']
        plan.save(update_fields=['stripe_price_id', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'{plan.code}: {price["id"]}'))

    def _sync_package(self, package):
        product = stripe.Product.create(
            name=f'Loopd\u2019n {package.name}',
            metadata={'credit_package_id': str(package.pk)},
        )
        price = stripe.Price.create(
            product=product['id'],
            unit_amount=package.price_cents,
            currency='usd',
            metadata={'credit_package_id': str(package.pk)},
        )
        package.stripe_price_id = price['id']
        package.save(update_fields=['stripe_price_id', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'{package.name}: {price["id"]}'))
