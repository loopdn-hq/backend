import uuid
from pathlib import Path

from django.db import models
from django.db.models import Q


def creative_image_upload_to(instance, filename):
    """Store a creative's image under an unguessable name.

    Same reasoning as `user.models.avatar_upload_to`: the uploaded filename is
    attacker-controlled and collides constantly, so only the suffix survives.

    `promote/`, not `ads/`: ad blockers match URLs containing `/ads/`, and this
    path ends up in the image URL an advertiser previews their own ad through.
    """
    suffix = Path(filename).suffix.lower() or '.jpg'
    return f'promote/media/{uuid.uuid4().hex}{suffix}'


class Creative(models.Model):
    """What a sponsored message actually says and shows.

    Only `approved` creatives are ever selected, and that is a queryset filter
    in ads/services.py rather than a check anybody can forget (plan, Stage 4).

    A call or text CTA carries no number of its own. It uses the campaign's
    tracking number (plan 2c), so no creative can route a response around the
    measurement - which is also why review rejects a body containing any other
    number.
    """

    class CtaKind(models.TextChoices):
        CALL = 'call', 'Call'
        TEXT = 'text', 'Text'
        # A web address - the website, a booking page, a lead form.
        LINK = 'link', 'Open a link'
        GROUP_JOIN = 'group_join', 'Join a group'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PENDING_REVIEW = 'pending_review', 'Pending review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    campaign = models.ForeignKey(
        'ads.Campaign', on_delete=models.CASCADE, related_name='creatives'
    )
    headline = models.CharField(max_length=80)
    body = models.CharField(max_length=500)
    image = models.ImageField(upload_to=creative_image_upload_to, blank=True)

    cta_label = models.CharField(max_length=30)
    cta_kind = models.CharField(max_length=16, choices=CtaKind.choices)
    # The URL for a link, the join link for a group. Blank for call and text.
    cta_target = models.CharField(max_length=500, blank=True, default='', db_default='')

    # Sponsored SMS only (plan, Stage 5), which is gated; nothing sends it yet.
    sms_body = models.CharField(max_length=320, blank=True, default='', db_default='')

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_default=Status.DRAFT,
    )
    review_notes = models.TextField(blank=True, default='', db_default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(cta_kind__in=['call', 'text'], cta_target='')
                    | (Q(cta_kind__in=['link', 'group_join']) & ~Q(cta_target=''))
                ),
                name='ads_creative_cta_target_matches_kind',
            ),
        ]
        indexes = [
            models.Index(fields=['campaign', 'status'], name='ads_creative_status_idx'),
        ]

    def __str__(self):
        return self.headline
