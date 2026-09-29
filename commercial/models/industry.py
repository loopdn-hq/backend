from django.db import models


class Industry(models.Model):
    """What a company does: food and drink, fitness, real estate.

    Asked for on "Set up your company", and read by campaign review and by
    reports. Not "business category": *Business* is a chat plan tier, and a
    *category* is what kind of group a conversation is (`chat.GroupCategory`).

    A table rather than choices for the same reason as GroupCategory: adding one
    should be a row, not a deploy. Seeded by migration 0004. `active=False`
    retires one without touching the companies that already chose it.
    """

    slug = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=80)
    active = models.BooleanField(default=True, db_default=True)
    # The order the setup form lists them in, with "Other" last.
    sort_order = models.PositiveSmallIntegerField(default=0, db_default=0)

    class Meta:
        ordering = ['sort_order', 'label']
        verbose_name_plural = 'industries'

    def __str__(self):
        return self.label
