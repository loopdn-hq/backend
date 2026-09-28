from django.core.cache import cache
from django.db import models


class AdSettings(models.Model):
    """The one row that stops every ad, immediately. Mirrors sms.SmsSettings.

    A database row and not an environment variable, for SmsSettings' reason:
    flipping an ECS variable is a task definition and a rolling deploy, and the
    day you need this switch is the day an ad is going somewhere it should not.

    It differs from SmsSettings in one way, on purpose: it fails CLOSED. With no
    row, there are no ads. SMS is the product and has to keep working through a
    missing row; ads are the one thing that must not appear until somebody
    turned them on. Migration 0002 seeds the row switched off.
    """

    CACHE_KEY = 'ads:settings:serving'

    singleton = models.BooleanField(
        primary_key=True,
        default=True,
        db_default=True,
        help_text='Always True. Enforced as the primary key so there is one row.',
    )
    serving_enabled = models.BooleanField(
        default=False,
        db_default=False,
        help_text='Turn on to let campaigns serve. Turn off to stop every ad at once; '
                  'takes effect within a minute.',
    )
    note = models.CharField(
        max_length=200,
        blank=True,
        default='',
        db_default='',
        help_text='Why it was last switched, for whoever finds it later.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'ad settings'
        verbose_name_plural = 'ad settings'

    def __str__(self):
        return 'serving on' if self.serving_enabled else 'serving OFF'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    @classmethod
    def serving_is_on(cls):
        """Whether any ad may be served right now. False when there is no row."""
        cached = cache.get(cls.CACHE_KEY)
        if cached is not None:
            return cached

        row = cls.objects.first()
        enabled = row is not None and row.serving_enabled
        cache.set(cls.CACHE_KEY, enabled, 60)
        return enabled
