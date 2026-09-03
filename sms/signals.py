"""Rule 3: deleting a group deletes its number.

A signal rather than a line in a delete endpoint, because a group can be deleted
from several places - the admin today, an API endpoint tomorrow, a management
command clearing test data - and a number that survives its group is one nobody
is watching and everybody is still paying for, every month.

pre_delete rather than post_delete: the GroupNumber row is found through its
FK to the conversation, and after the delete that link is gone.
"""
import logging

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from chat.models import Conversation

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=Conversation)
def release_group_number(sender, instance, **kwargs):
    if instance.kind != Conversation.Kind.GROUP:
        return

    from sms.provisioning import release_for

    try:
        release_for(instance)
    except Exception:
        # Never block a deletion on a provider being unreachable. The number is
        # logged loudly so it can be released by hand rather than silently
        # billed forever.
        logger.exception(
            'Could not release the number for conversation %s; release it manually',
            instance.pk,
        )
