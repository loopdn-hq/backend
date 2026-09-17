"""One browser that has agreed to be notified."""
from django.conf import settings
from django.db import models


class PushSubscription(models.Model):
    """A browser's own address for push, and the keys that encrypt to it.

    ONE ROW PER BROWSER, not per account. Somebody signed in on a laptop and a
    phone has two, and both are sent to: a notification that arrived only on
    whichever device asked last would be worse than none, because it would look
    like it worked.

    `endpoint` is the URL the browser's own push service listens on - Google's,
    Mozilla's or Apple's, depending on the browser - and it is the identity of
    the subscription. It is unique across the table rather than per user, so
    the same browser signing in as somebody else MOVES to that account instead
    of quietly delivering one person's messages to another person's screen.

    The two keys are the browser's public key and an authentication secret; the
    payload is encrypted to them, so nothing readable crosses the push service.
    They are useless without the browser that made them, which is why they can
    live here in plain columns while the VAPID private key may not.

    A subscription is not permanent and needs no sweep: a browser that has been
    uninstalled, cleared or left idle for months answers 404 or 410, and
    `notification/push/outbound.py` deletes the row the first time that happens.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='push_subscriptions',
    )

    # Long on purpose. These are provider URLs with an opaque id on the end;
    # Apple's run past 300 characters, and a truncated one is undeliverable in
    # a way that looks like a silent failure.
    endpoint = models.CharField(max_length=500, unique=True)

    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)

    # What this browser calls itself, for the "which device is this" line on
    # the Notifications screen. Display only; never matched on.
    user_agent = models.CharField(max_length=200, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    # When something was last delivered here, so a support question about a
    # phone that stopped buzzing has an answer other than a shrug.
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The only query there is: every browser belonging to one person.
            models.Index(fields=['user']),
        ]

    def __str__(self):
        return f'{self.user_id} @ {self.endpoint[:40]}...'

    @property
    def subscription_info(self):
        """The shape pywebpush expects, which is the shape the browser gave."""
        return {
            'endpoint': self.endpoint,
            'keys': {'p256dh': self.p256dh, 'auth': self.auth},
        }
