from django.db import models
from django.db.models import F, Q


class TargetingCriteria(models.Model):
    """Which groups a campaign may appear in: by what the groups ARE, never by
    what their members SAY.

    Category and coarse region only. §13 of the product plan allows targeting on
    "permitted aggregate information" and forbids using conversation content,
    and both fields here are declared by a group's admin rather than worked out
    from anything anybody wrote. An empty list means "any".
    """

    campaign = models.OneToOneField(
        'commercial.Campaign', on_delete=models.CASCADE, related_name='targeting'
    )
    categories = models.ManyToManyField(
        'chat.GroupCategory', blank=True, related_name='+'
    )
    # ISO 3166 codes: a country ("PH") or a country and its first-level
    # subdivision ("US-CA"). Never finer - a postcode on a small group is not
    # aggregate, and §13 would not allow it.
    regions = models.JSONField(default=list, blank=True)

    min_group_size = models.PositiveIntegerField(null=True, blank=True)
    max_group_size = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'targeting criteria'
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(min_group_size__isnull=True)
                    | Q(max_group_size__isnull=True)
                    | Q(max_group_size__gte=F('min_group_size'))
                ),
                name='commercial_targetingcriteria_min_within_max',
            ),
        ]

    def __str__(self):
        return f'targeting for {self.campaign}'
