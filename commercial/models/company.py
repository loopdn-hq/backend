import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q

from commercial.markets import Country, Currency
from user.models import phone_number_validator


def company_logo_upload_to(instance, filename):
    """Store a logo under an unguessable name, like a creative's image.

    The same private storage and the same folder: `commercial/`, never `ads/`,
    because ad blockers match URLs containing `/ads/`.
    """
    suffix = Path(filename).suffix.lower() or '.png'
    return f'commercial/media/{uuid.uuid4().hex}{suffix}'


class CompanyQuerySet(models.QuerySet):
    def manageable_by(self, user):
        """The companies this person may see and change. Every lookup starts here.

        Somebody else's company is then simply not found - a 404, never a 403
        that confirms it exists. Today that means the ones they own; people
        managing a company they do not own (docs/rbac_plan.md, step 9) widen
        this one query and nothing else.

        Suspended companies are included: their owner still needs to see them,
        and what a suspended company may not do is the services' business.
        """
        if not getattr(user, 'is_authenticated', False):
            return self.none()
        return self.filter(owner=user)


class Company(models.Model):
    """A business that buys ads - not the person who signed in to buy them.

    The product plan (§11-§13) calls this "the advertiser"; here it is a
    company, the word the commercial dashboard shows. One person can run
    several (two shops, an agency with clients), and later several people will
    manage one (docs/rbac_plan.md, step 9), so campaigns belong here and never
    to a User. Suspending a company stops its ads and
    touches nothing else its owner does: `user.is_active` would lock them out of
    their family group as well.

    The money lives here too. It is one-to-one with the business, so a separate
    account table would add a join and nothing else - and it is NOT
    `billing.Wallet`, which is one per user and means prepaid SMS/voice credit.
    Two meanings of "wallet" in one codebase is how a refund ends up paying for
    somebody's SMS. `AdTransaction` is the ledger; `balance_cents` is where it
    has got to, kept so "can this campaign still run" is one read.

    `owner` is PROTECT: deleting the person must not silently take a business
    that holds money, and its spending history, with them.
    """

    class Status(models.TextChoices):
        # From the moment it is created (plan decision 18): Loopd'n reviews
        # campaigns, not companies, so there is nothing to wait for.
        ACTIVE = 'active', 'Active'
        # Every campaign stops. The owner keeps using the rest of the product.
        SUSPENDED = 'suspended', 'Suspended'

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='companies',
    )
    # What a sponsored message is labelled with: "Sponsored · Joe's Pizza".
    name = models.CharField(max_length=120)
    # PROTECT: retiring an industry is `active=False`, never a delete.
    industry = models.ForeignKey(
        'commercial.Industry', on_delete=models.PROTECT, related_name='companies'
    )
    # The market it advertises in. See commercial/markets.py.
    country = models.CharField(max_length=2, choices=Country.choices)
    # Every amount for this company - balance, budgets, spend - is in this.
    # Fixed once any money has moved: services/companies.update_company.
    currency = models.CharField(max_length=3, choices=Currency.choices)
    website = models.URLField(blank=True, default='', db_default='')
    contact_email = models.EmailField(blank=True, default='', db_default='')
    # Optional, and not asked for on setup (plan decision 20): filled in later,
    # on the company's details. International format, like a profile's number.
    contact_phone = models.CharField(
        max_length=16,
        blank=True,
        default='',
        db_default='',
        validators=[phone_number_validator],
    )
    address = models.CharField(max_length=255, blank=True, default='', db_default='')
    # The company in its own words. Read by campaign review; never shown to
    # members as it stands.
    description = models.CharField(
        max_length=500, blank=True, default='', db_default=''
    )
    logo = models.ImageField(upload_to=company_logo_upload_to, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_default=Status.ACTIVE,
    )

    # Cents, like billing.Wallet, so pricing is integer arithmetic. Two
    # buckets (docs/campaigns_final_plan.md, section 9): `balance_cents` is
    # AVAILABLE - free to commit to a new campaign - and `reserved_cents` is
    # committed to submitted campaigns and not yet spent. Only
    # services/wallet.py writes either.
    balance_cents = models.IntegerField(default=0, db_default=0)
    reserved_cents = models.IntegerField(default=0, db_default=0)
    # The business's own Stripe customer, not its owner's (that one is on
    # billing.Subscription): receipts carry the company's name and tax details,
    # and the card on file is the business's, not the person's.
    stripe_customer_id = models.CharField(
        max_length=255, blank=True, default='', db_default=''
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CompanyQuerySet.as_manager()

    class Meta:
        ordering = ['name']
        constraints = [
            # The ledger says how the balance got here; this is the backstop
            # that stops a bug ever spending money the company does not have.
            models.CheckConstraint(
                condition=Q(balance_cents__gte=0),
                name='commercial_company_balance_non_negative',
            ),
            models.CheckConstraint(
                condition=Q(reserved_cents__gte=0),
                name='commercial_company_reserved_non_negative',
            ),
            # Two companies on one Stripe customer would pay for each other's
            # campaigns. Blank means "no customer yet" and may repeat.
            models.UniqueConstraint(
                fields=['stripe_customer_id'],
                condition=~Q(stripe_customer_id=''),
                name='commercial_company_unique_stripe_customer',
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def money_has_moved(self):
        """Whether any ledger row exists - after which the currency is fixed."""
        return self.transactions.exists()
