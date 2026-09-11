"""Create/update the Stripe Products and Prices behind every Plan and
CreditPackage, and write the resulting price ids back onto those rows.

Idempotent: reruns update the existing Product/Price by the id already
stored rather than creating duplicates, so it is safe to call again after
editing a plan's price in the admin (Stripe prices are immutable - this
retires the old one and mints a replacement).

    python manage.py sync_stripe_billing
"""
import json

import stripe
from django.core.management.base import BaseCommand

from billing.models import CreditPackage, Plan


class Command(BaseCommand):
    help = 'Create or update Stripe Products/Prices for every Plan and CreditPackage.'

    def handle(self, *args, **options):
        plan_qs = Plan.objects.exclude(code=Plan.Code.FREE).filter(is_custom_pricing=False)
        package_qs = CreditPackage.objects.filter(active=True)

        plan_count = plan_qs.count()
        package_count = package_qs.count()
        processed_plans = 0
        processed_packages = 0

        self.stdout.write(
            f'Found {plan_count} eligible plan(s) and {package_count} active credit package(s).'
        )

        for plan in plan_qs:
            self._sync_plan(plan)
            processed_plans += 1
        for package in package_qs:
            self._sync_package(package)
            processed_packages += 1

        if processed_plans == 0 and processed_packages == 0:
            self.stdout.write(self.style.WARNING(
                'Nothing to sync. Create a paid non-custom plan and/or an active credit package, then rerun.'
            ))

        self.stdout.write(self.style.SUCCESS(
            f'Sync complete. Plans processed: {processed_plans}. Packages processed: {processed_packages}.'
        ))

    def _sync_plan(self, plan):
        if plan.monthly_price_cents is None:
            self.stdout.write(self.style.WARNING(
                f'Skipping {plan.code}: no monthly_price_cents set.'
            ))
            return

        metadata = self._plan_metadata(plan)
        price, changed, action = self._sync_catalog_price(
            current_price_id=plan.stripe_price_id,
            product_name=f"Loopd'n {plan.name}",
            product_metadata=metadata,
            price_kwargs={
                'unit_amount': plan.monthly_price_cents,
                'currency': 'usd',
                'recurring': {'interval': 'month'},
                'metadata': metadata,
            },
        )
        if changed:
            plan.stripe_price_id = price['id']
            plan.save(update_fields=['stripe_price_id', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'{plan.code}: {price["id"]} ({action})'))

    def _sync_package(self, package):
        metadata = self._credit_metadata(package)
        price, changed, action = self._sync_catalog_price(
            current_price_id=package.stripe_price_id,
            product_name=f"Loopd'n {package.name}",
            product_metadata=metadata,
            price_kwargs={
                'unit_amount': package.price_cents,
                'currency': 'usd',
                'metadata': metadata,
            },
        )
        if changed:
            package.stripe_price_id = price['id']
        product_id = self._extract_product_id(price.get('product'))
        if package.stripe_product_id != product_id:
            package.stripe_product_id = product_id
            changed = True
        if changed:
            package.save(update_fields=['stripe_price_id', 'stripe_product_id', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'{package.name}: {price["id"]} ({action})'))

    def _sync_catalog_price(self, current_price_id, product_name, product_metadata, price_kwargs):
        current_price = None
        if current_price_id:
            current_price = self._safe_retrieve_price(current_price_id)

        preferred_product_id = current_price['product'] if current_price else ''
        product = self._get_or_create_product(
            name=product_name,
            metadata=product_metadata,
            preferred_product_id=preferred_product_id,
        )

        desired_kwargs = dict(price_kwargs)
        desired_kwargs['product'] = product['id']

        if current_price and self._price_matches(current_price, desired_kwargs):
            return current_price, False, 'unchanged'

        if current_price:
            stripe.Price.modify(current_price['id'], active=False)

        new_price = stripe.Price.create(**desired_kwargs)
        return new_price, True, 'created' if not current_price else 'rotated'

    def _safe_retrieve_price(self, price_id):
        try:
            return self._to_dict(stripe.Price.retrieve(price_id))
        except stripe.InvalidRequestError:
            self.stdout.write(self.style.WARNING(
                f'Configured Stripe price {price_id} does not exist; creating a new one.'
            ))
            return None

    def _get_or_create_product(self, name, metadata, preferred_product_id=''):
        product = None

        if preferred_product_id:
            try:
                product = stripe.Product.retrieve(preferred_product_id)
            except stripe.InvalidRequestError:
                product = None

        if product is None:
            product = self._find_product_by_metadata(metadata)

        if product is None:
            product = stripe.Product.create(name=name, metadata=metadata)
        else:
            product = stripe.Product.modify(product['id'], name=name, metadata=metadata)

        return product

    def _find_product_by_metadata(self, metadata):
        key, value = next(iter(metadata.items()))
        for product in stripe.Product.list(limit=100, active=True).auto_paging_iter():
            product_data = self._to_dict(product)
            if (product_data.get('metadata') or {}).get(key) == value:
                return product
        return None

    def _price_matches(self, current_price, desired_kwargs):
        if not current_price.get('active', False):
            return False
        if current_price.get('product') != desired_kwargs['product']:
            return False
        if current_price.get('unit_amount') != desired_kwargs['unit_amount']:
            return False
        if str(current_price.get('currency', '')).lower() != str(desired_kwargs['currency']).lower():
            return False

        current_recurring = current_price.get('recurring') or {}
        desired_recurring = desired_kwargs.get('recurring') or {}
        if bool(current_recurring) != bool(desired_recurring):
            return False
        if desired_recurring:
            if current_recurring.get('interval') != desired_recurring.get('interval'):
                return False
            if current_recurring.get('interval_count', 1) != desired_recurring.get('interval_count', 1):
                return False

        current_metadata = current_price.get('metadata') or {}
        desired_metadata = desired_kwargs.get('metadata') or {}
        for key, value in desired_metadata.items():
            if current_metadata.get(key) != value:
                return False

        return True

    def _to_dict(self, stripe_obj):
        if stripe_obj is None:
            return None
        if isinstance(stripe_obj, dict):
            return stripe_obj
        if hasattr(stripe_obj, 'to_dict_recursive'):
            return stripe_obj.to_dict_recursive()
        if hasattr(stripe_obj, 'to_dict'):
            return stripe_obj.to_dict()
        return stripe_obj

    def _extract_product_id(self, value):
        if isinstance(value, dict):
            return str(value.get('id') or '')
        return str(value or '')

    def _plan_metadata(self, plan):
        payload = {
            'type': 'plan',
            'code': plan.code,
            'max_groups': plan.max_groups,
            'max_members_per_group': plan.max_members_per_group,
            'max_admins_per_group': plan.max_admins_per_group,
            'storage_gb': plan.storage_gb,
            'has_whatsapp': plan.has_whatsapp,
            'has_telegram': plan.has_telegram,
            'features': plan.features or [],
            'is_custom_pricing': plan.is_custom_pricing,
        }
        return {
            'plan_code': plan.code,
            'data': json.dumps(payload, separators=(',', ':')),
        }

    def _credit_metadata(self, package):
        payload = {
            'type': 'credit',
            'id': package.pk,
        }
        return {
            'credit_package_id': str(package.pk),
            'data': json.dumps(payload, separators=(',', ':')),
        }
