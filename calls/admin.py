from django.contrib import admin

from calls.models import CallEvent, CallParticipant, CallRecording, CallRoom


class CallParticipantInline(admin.TabularInline):
    model = CallParticipant
    extra = 0
    fields = ('user', 'joined_at', 'connected_at', 'left_at', 'duration')
    readonly_fields = fields
    can_delete = False
    raw_id_fields = ('user',)


class CallRecordingInline(admin.TabularInline):
    model = CallRecording
    extra = 0
    # No s3 fields: where a recording lives is not something to read off a
    # screen, and the admin has no reason to hand out a link to it.
    fields = ('status', 'started_by', 'started_at', 'ended_at', 'duration_seconds', 'expires_at')
    readonly_fields = fields
    can_delete = False


@admin.register(CallRoom)
class CallRoomAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'conversation', 'created_by', 'call_type', 'status', 'end_reason',
        'created_at', 'ended_at',
    )
    list_filter = ('status', 'call_type', 'end_reason')
    search_fields = ('room_name', 'created_by__username')
    raw_id_fields = ('conversation', 'created_by', 'ended_by')
    readonly_fields = ('room_name', 'created_at')
    inlines = [CallParticipantInline, CallRecordingInline]


@admin.register(CallEvent)
class CallEventAdmin(admin.ModelAdmin):
    list_display = ('call_room', 'type', 'user', 'created_at')
    list_filter = ('type',)
    raw_id_fields = ('call_room', 'user')
