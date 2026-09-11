"""Admin for conversations and messages.

Message bodies are private, so this is deliberately thin: enough to see that
a conversation exists and who is in it, without turning the admin into a
window onto everybody's chats.
"""
from django.contrib import admin

from chat.models import Attachment, Conversation, Message, Participant


class ParticipantInline(admin.TabularInline):
    model = Participant
    extra = 0
    fields = [
        'user',
        'phone_number',
        'display_name',
        'role',
        'sms_enabled',
        'joined_at',
    ]
    readonly_fields = ['joined_at']
    autocomplete_fields = ['user']
    # `added_by` is a second FK to User on this model, so the inline needs to be
    # told which one joins it to the parent.
    fk_name = 'conversation'


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'kind', 'created_by', 'created_at', 'updated_at']
    list_filter = ['kind']
    search_fields = ['name', 'participants__user__username']
    readonly_fields = ['direct_key', 'created_at', 'updated_at']
    inlines = [ParticipantInline]


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0
    readonly_fields = ['kind', 'original_name', 'content_type', 'size', 'width', 'height']


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'conversation', 'sender_label', 'source', 'created_at']
    list_filter = ['source', 'created_at']
    search_fields = ['sender__username']
    readonly_fields = [
        'conversation',
        'sender',
        'sender_participant',
        'source',
        'text',
        'created_at',
        'updated_at',
    ]
    inlines = [AttachmentInline]

    def has_add_permission(self, request):
        return False
