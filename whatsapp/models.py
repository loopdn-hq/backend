"""WhatsApp as a channel on a Loopd'n group, over the owner's own business number.

The shape, from the product plan (Option C):

    User -> WhatsAppConnection -> GroupChannel -> Conversation (a group)

A group owner connects THEIR WhatsApp Business Account through Meta's Embedded
Signup. That number then carries the groups they attach it to. The group itself
stays ours - membership, history, admins, consent - and WhatsApp is one more way
into it, exactly as SMS and email are. Nothing here creates a native WhatsApp
group, and nothing here should: WhatsApp is not the group database.

Five tables, each with one job:

    WhatsAppConnection    one business phone number a user has connected, and
                          the credential that lets us send from it.
    GroupChannel          which groups a connection carries.
    WhatsAppContact       one person as seen by one business number: when they
                          last wrote (the 24-hour window) and which group their
                          next message belongs to.
    WhatsAppDelivery      one message per recipient. The support and audit log.
    WhatsAppWebhookEvent  which Meta events have been handled, so a retry does
                          not post the same message into a group twice.

WHY THE CONNECTION IS NOT ON THE CONVERSATION
---------------------------------------------
One business number can serve many groups - a coach's number for every team
they run - and it has a lifecycle of its own: it is connected before any group
exists, it can be disconnected by its owner in Meta's Business Manager without
telling us, and it outlives every group it served. A column on Conversation
could express none of that.
"""
import re
import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q

from chat.models import Conversation, Message, Participant


class WhatsAppConnection(models.Model):
    """One WhatsApp Business phone number, connected by the person who owns it.

    Created by Embedded Signup (whatsapp/onboarding.py). The owner is the
    Loopd'n account that went through Meta's flow; only they may attach it to a
    group, and only to a group they administer.

    THE TOKEN IS NEVER READABLE IN PLAIN TEXT AT REST. `access_token_encrypted`
    holds a Fernet token (whatsapp/crypto.py) and the only way out is
    `access_token`, which nothing serializes. A database dump, a backup, or the
    admin must not be a way to post as somebody's business.
    """

    class Status(models.TextChoices):
        # Connected and able to send.
        ACTIVE = 'active', 'Active'
        # The owner disconnected it here, or removed our app in Meta's
        # Business Manager (the `account_update` webhook). Nothing is sent.
        DISCONNECTED = 'disconnected', 'Disconnected'
        # Staff stopped it from the admin - a runaway loop, a policy complaint.
        # Distinct from DISCONNECTED so an owner cannot simply reconnect past
        # it: only staff lift a suspension.
        SUSPENDED = 'suspended', 'Suspended'
        # Meta refused a call in a way that will not fix itself: the token was
        # revoked, the number was deregistered. Needs the owner to reconnect.
        ERROR = 'error', 'Error'

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='whatsapp_connections',
    )

    # Meta's identifiers. Opaque to us, and all we need to address the API.
    waba_id = models.CharField(max_length=64)
    phone_number_id = models.CharField(max_length=64)
    business_id = models.CharField(max_length=64, blank=True, default='', db_default='')

    # What Meta says the number is, refreshed on connect and on demand. For
    # display only - the routing key is `phone_number_id`, which is what every
    # webhook carries.
    display_phone_number = models.CharField(
        max_length=32, blank=True, default='', db_default=''
    )
    verified_name = models.CharField(
        max_length=128, blank=True, default='', db_default=''
    )
    quality_rating = models.CharField(
        max_length=16, blank=True, default='', db_default=''
    )
    messaging_limit_tier = models.CharField(
        max_length=32, blank=True, default='', db_default=''
    )

    access_token_encrypted = models.TextField(blank=True, default='', db_default='')
    # Null for a system-user token, which is what Embedded Signup issues and
    # which does not expire. Kept because a user token does, and the day we
    # accept one we want to know when it stops working.
    token_expires_at = models.DateTimeField(null=True, blank=True)

    # The approved template used when a recipient is outside WhatsApp's
    # 24-hour customer service window, and free-form text is refused. Per
    # connection because a template belongs to a WABA: one tenant's approved
    # template does not exist on another's account. Blank falls back to
    # settings.WHATSAPP_DEFAULT_TEMPLATE_NAME.
    #
    # The template must take three body parameters, in this order:
    #     {{1}} group name, {{2}} sender, {{3}} message text
    notification_template_name = models.CharField(
        max_length=512, blank=True, default='', db_default=''
    )
    notification_template_language = models.CharField(
        max_length=16, blank=True, default='', db_default=''
    )
    # The approved template that asks somebody to opt in, sent when an admin
    # invites them to a group over WhatsApp. Business-initiated, so it can only
    # ever be a template. Two body parameters: {{1}} inviter, {{2}} group name.
    # Blank falls back to settings.WHATSAPP_INVITE_TEMPLATE_NAME.
    invitation_template_name = models.CharField(
        max_length=512, blank=True, default='', db_default=''
    )

    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_default=Status.ACTIVE,
    )
    # The last thing Meta told us went wrong, for the owner's settings screen
    # and for support. Never a token, never a payload.
    last_error = models.CharField(max_length=255, blank=True, default='', db_default='')

    connected_at = models.DateTimeField(auto_now_add=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-connected_at']
        constraints = [
            # One live connection per number, across every tenant. Two would
            # mean an inbound webhook with two possible owners, and whoever
            # connected second could read the first owner's groups' replies.
            # A DISCONNECTED row stays behind as history and does not collide.
            models.UniqueConstraint(
                fields=['phone_number_id'],
                condition=~Q(status='disconnected'),
                name='one_live_whatsapp_connection_per_number',
            ),
        ]
        indexes = [models.Index(fields=['owner', 'status'])]

    def __str__(self):
        label = self.verified_name or self.display_phone_number or self.phone_number_id
        return f'{label} ({self.get_status_display()})'

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def access_token(self):
        """The plain token, for the one place that talks to Meta."""
        from whatsapp import crypto

        return crypto.decrypt(self.access_token_encrypted)

    def set_access_token(self, token):
        from whatsapp import crypto

        self.access_token_encrypted = crypto.encrypt(token) if token else ''

    @property
    def template_name(self):
        return self.notification_template_name or settings.WHATSAPP_DEFAULT_TEMPLATE_NAME

    @property
    def invite_template_name(self):
        return self.invitation_template_name or settings.WHATSAPP_INVITE_TEMPLATE_NAME

    @property
    def template_language(self):
        return (
            self.notification_template_language
            or settings.WHATSAPP_DEFAULT_TEMPLATE_LANGUAGE
        )

    @classmethod
    def for_phone_number_id(cls, phone_number_id):
        """The live connection a webhook for this number belongs to, or None.

        ACTIVE only. A disconnected number's messages are not ours to post
        anywhere, even if Meta is still delivering them to our app.
        """
        if not phone_number_id:
            return None
        return cls.objects.filter(
            phone_number_id=phone_number_id, status=cls.Status.ACTIVE
        ).first()


_KEYWORD_RE = re.compile(r'[^a-z0-9]+')


def keyword_from(name):
    """A short, typeable handle for a group, from its name. `Family Chat` -> `familychat`."""
    return _KEYWORD_RE.sub('', (name or '').lower())[:16]


class GroupChannel(models.Model):
    """One group, carried over one external channel.

    The join the product plan calls "Group Channels": Web is implicit, SMS has
    its own number table (sms.GroupNumber) for reasons given there, and this is
    where WhatsApp - and later anything else that is bring-your-own-account -
    attaches to a group.

    `keyword` is how a person who belongs to several groups on the SAME business
    number says which one they mean: `#family see you at 7`. Unique per
    connection, since that is the scope in which it has to be unambiguous.
    """

    class Kind(models.TextChoices):
        WHATSAPP = 'whatsapp', 'WhatsApp'

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='group_channels',
    )
    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.WHATSAPP,
        db_default=Kind.WHATSAPP,
    )
    whatsapp_connection = models.ForeignKey(
        WhatsAppConnection,
        on_delete=models.CASCADE,
        related_name='group_channels',
    )
    keyword = models.CharField(max_length=16)

    # OUTBOUND: messages posted in the group go out on WhatsApp.
    delivery_enabled = models.BooleanField(default=True, db_default=True)
    # INBOUND: WhatsApp messages from members are posted into the group.
    replies_enabled = models.BooleanField(default=True, db_default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            # One WhatsApp number per group. Two would send every message
            # twice, from two identities, and replies would split between them.
            models.UniqueConstraint(
                fields=['conversation', 'kind'], name='one_channel_of_a_kind_per_group'
            ),
            models.UniqueConstraint(
                fields=['whatsapp_connection', 'keyword'],
                name='unique_keyword_per_whatsapp_connection',
            ),
        ]

    def __str__(self):
        return f'{self.conversation} via {self.whatsapp_connection}'

    @property
    def is_live(self):
        """Whether this channel can carry anything right now."""
        return self.whatsapp_connection.is_active

    @classmethod
    def whatsapp_for(cls, conversation):
        """The live WhatsApp channel of this group, or None."""
        return (
            cls.objects.select_related('whatsapp_connection')
            .filter(
                conversation=conversation,
                kind=cls.Kind.WHATSAPP,
                whatsapp_connection__status=WhatsAppConnection.Status.ACTIVE,
            )
            .first()
        )

    @classmethod
    def unused_keyword(cls, connection, name):
        """A keyword for `name` that no other group on this connection holds."""
        base = keyword_from(name) or 'group'
        taken = set(
            cls.objects.filter(whatsapp_connection=connection)
            .values_list('keyword', flat=True)
        )
        if base not in taken:
            return base
        for _ in range(20):
            candidate = f'{base[:12]}{secrets.randbelow(10_000):04d}'
            if candidate not in taken:
                return candidate
        return secrets.token_hex(8)


class WhatsAppContact(models.Model):
    """One person, as one business number knows them.

    Two facts live here and nowhere else, and both are per business number
    rather than per group, because that is how WhatsApp sees them:

    `last_inbound_at` opens Meta's 24-hour customer service window. Inside it
    we may send free-form text; outside it only an approved template. The
    window belongs to the (business number, person) pair - a message to one of
    the owner's groups opens it for all of them.

    `current_conversation` is where this person's next message goes when they
    are in several groups on this number and did not say which. It follows
    them: it is set when they name a group, and when a group's message reaches
    them, so a plain reply lands in the group they were just reading.

    The number is stored as Meta's `wa_id` (digits, no plus). It is the key
    Meta gives us and the key it expects back.
    """

    connection = models.ForeignKey(
        WhatsAppConnection, on_delete=models.CASCADE, related_name='contacts'
    )
    wa_id = models.CharField(max_length=20)
    profile_name = models.CharField(max_length=128, blank=True, default='', db_default='')

    last_inbound_at = models.DateTimeField(null=True, blank=True)
    current_conversation = models.ForeignKey(
        Conversation,
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['connection', 'wa_id'], name='unique_whatsapp_contact'
            ),
        ]

    def __str__(self):
        return f'...{self.wa_id[-4:]} on {self.connection_id}'


class WhatsAppWebhookEvent(models.Model):
    """One thing Meta told us that we have already acted on.

    Meta batches: one POST can carry several messages and status updates, and a
    retry resends the whole batch. So the claim is per ITEM, keyed on what that
    item is - `msg:<wamid>` for an inbound message, `status:<wamid>:<status>`
    for a status - never on the request. A batch that failed halfway is then
    safe to replay: what was done is skipped, what was not is done.
    """

    key = models.CharField(max_length=200, unique=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [models.Index(fields=['received_at'])]

    def __str__(self):
        return self.key


class WhatsAppDelivery(models.Model):
    """One message, to or from one person, over WhatsApp.

    The same shape as sms.SmsDelivery and for the same reasons: "why did I not
    get that" is a support question, and the log answers it with a reason
    rather than a silence. The counterpart's number is a hash and a last-four,
    as there - this table would otherwise be a permanent record of who talks to
    which private group.

    Unlike SMS, the bill for these is Meta's to the group owner, not ours: the
    messages go out on their WABA. `pricing_category` is kept so an owner asking
    what they were charged for has an answer.
    """

    class Direction(models.TextChoices):
        INBOUND = 'inbound', 'Inbound'
        OUTBOUND = 'outbound', 'Outbound'

    class Kind(models.TextChoices):
        # Free-form: only inside the 24-hour window.
        TEXT = 'text', 'Text'
        # An approved template: the only thing allowed outside it.
        TEMPLATE = 'template', 'Template'

    class Status(models.TextChoices):
        RECEIVED = 'received', 'Received'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        DELIVERED = 'delivered', 'Delivered'
        READ = 'read', 'Read'
        FAILED = 'failed', 'Failed'
        # Deliberately not sent. The reason says why.
        SKIPPED = 'skipped', 'Skipped'

    direction = models.CharField(max_length=8, choices=Direction.choices)
    connection = models.ForeignKey(
        WhatsAppConnection,
        on_delete=models.SET_NULL,
        related_name='deliveries',
        null=True,
        blank=True,
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='whatsapp_deliveries'
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        related_name='whatsapp_deliveries',
        null=True,
        blank=True,
    )
    participant = models.ForeignKey(
        Participant,
        on_delete=models.SET_NULL,
        related_name='whatsapp_deliveries',
        null=True,
        blank=True,
    )

    counterpart_last4 = models.CharField(max_length=4, blank=True, default='', db_default='')
    counterpart_hash = models.CharField(max_length=64, blank=True, default='', db_default='')

    kind = models.CharField(
        max_length=8, choices=Kind.choices, blank=True, default='', db_default=''
    )
    status = models.CharField(
        max_length=9,
        choices=Status.choices,
        default=Status.SENDING,
        db_default=Status.SENDING,
    )
    reason = models.CharField(max_length=160, blank=True, default='', db_default='')

    # Meta's id for the message. Indexed, not unique: statuses for one message
    # arrive several times. Also how a reply-to-this-message is routed back to
    # its group - see whatsapp/routing.py.
    wamid = models.CharField(
        max_length=128, blank=True, default='', db_default='', db_index=True
    )
    pricing_category = models.CharField(
        max_length=32, blank=True, default='', db_default=''
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # One outbound row per message per recipient: a retried fan-out
            # collides instead of messaging somebody twice.
            models.UniqueConstraint(
                fields=['message', 'participant'],
                condition=Q(direction='outbound'),
                name='one_outbound_whatsapp_per_recipient',
            ),
        ]
        indexes = [
            models.Index(fields=['conversation', '-created_at']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f'{self.direction} {self.status} ...{self.counterpart_last4}'
