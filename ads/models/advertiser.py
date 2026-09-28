from django.conf import settings
from django.db import models
from django.db.models import Q


class Advertiser(models.Model):
    """A business that buys ads - not the person who signed in to buy them.

    One person can run several (two shops, an agency with clients), and later
    several people will manage one (docs/rbac_plan.md, step 9), so campaigns
    belong here and never to a User. Suspending an advertiser stops its ads and
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
        # Signed up. Nothing it creates runs until somebody at Loopd'n looks.
        PENDING = 'pending', 'Pending review'
        ACTIVE = 'active', 'Active'
        # Every campaign stops. The owner keeps using the rest of the product.
        SUSPENDED = 'suspended', 'Suspended'

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='advertisers',
    )
    # What a sponsored message is labelled with: "Sponsored · Joe's Pizza".
    name = models.CharField(max_length=120)
    website = models.URLField(blank=True, default='', db_default='')
    contact_email = models.EmailField(blank=True, default='', db_default='')
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_default=Status.PENDING,
    )

    # Cents, like billing.Wallet, so pricing is integer arithmetic.
    balance_cents = models.IntegerField(default=0, db_default=0)
    # The business's own Stripe customer, not its owner's (that one is on
    # billing.Subscription): receipts carry the company's name and tax details,
    # and the card on file is the business's, not the person's.
    stripe_customer_id = models.CharField(
        max_length=255, blank=True, default='', db_default=''
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            # The ledger says how the balance got here; this is the backstop
            # that stops a bug ever spending money the advertiser does not have.
            models.CheckConstraint(
                condition=Q(balance_cents__gte=0),
                name='ads_advertiser_balance_non_negative',
            ),
            # Two advertisers on one Stripe customer would pay for each other's
            # campaigns. Blank means "no customer yet" and may repeat.
            models.UniqueConstraint(
                fields=['stripe_customer_id'],
                condition=~Q(stripe_customer_id=''),
                name='ads_advertiser_unique_stripe_customer',
            ),
        ]

    def __str__(self):
        return self.name
