from django.conf import settings
from django.db import models


class CommercialPreference(models.Model):
    """Which of their companies a person is working in on the dashboard.

    Context, never authorization (docs/campaigns_final_plan.md, section 5):
    every company-scoped request names its company and is checked on its own,
    and nothing reads this to decide whose data to return. It only answers
    "which company should the dashboard open on", so two browser tabs on two
    companies cannot mix, and losing access to the stored company just means
    `resolve_active_company` picks another.

    On the server rather than in the Django session: the API is JWT, and the
    commercial site is another origin with no session cookie to keep it in.

    Its own table rather than a column on Profile, which belongs to chat.
    SET_NULL, so deleting a company leaves the person with no choice stored
    rather than taking this row with it.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='commercial_preference',
    )
    active_company = models.ForeignKey(
        'commercial.Company',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        # Nothing should ask a company who has it open.
        related_name='+',
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user} → {self.active_company or "no company"}'
