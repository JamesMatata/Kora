from django.contrib import admin

from communications.models import (
    BroadcastNotice,
    ConversationSession,
    MessageLog,
    ParentContact,
)


class MessageLogInline(admin.TabularInline):
    model = MessageLog
    extra = 0
    fields = (
        'direction',
        'sender',
        'body',
        'delivery_status',
        'twilio_message_sid',
        'created_at',
    )
    readonly_fields = ('created_at',)
    show_change_link = True


@admin.register(ParentContact)
class ParentContactAdmin(admin.ModelAdmin):
    list_display = (
        'phone_number',
        'parent_name',
        'is_verified',
        'reminders_paused_at',
        'last_contacted_at',
        'school',
        'created_at',
    )
    list_filter = ('is_verified', 'school')
    search_fields = ('phone_number', 'parent_name')
    autocomplete_fields = ('school', 'students')
    filter_horizontal = ('students',)
    readonly_fields = (
        'last_contacted_at',
        'reminders_paused_at',
        'created_at',
        'updated_at',
    )


@admin.register(ConversationSession)
class ConversationSessionAdmin(admin.ModelAdmin):
    list_display = (
        'parent_contact',
        'status',
        'active_student',
        'assigned_staff',
        'last_message_at',
        'school',
    )
    list_filter = ('status', 'school')
    search_fields = (
        'parent_contact__phone_number',
        'parent_contact__parent_name',
        'active_student__admission_number',
    )
    autocomplete_fields = (
        'parent_contact',
        'active_student',
        'assigned_staff',
        'school',
    )
    inlines = (MessageLogInline,)
    readonly_fields = ('last_message_at', 'created_at')


@admin.register(MessageLog)
class MessageLogAdmin(admin.ModelAdmin):
    list_display = (
        'session',
        'direction',
        'sender',
        'delivery_status',
        'twilio_message_sid',
        'created_at',
        'school',
    )
    list_filter = ('direction', 'sender', 'delivery_status', 'school')
    search_fields = (
        'body',
        'twilio_message_sid',
        'session__parent_contact__phone_number',
    )
    autocomplete_fields = ('session', 'school')
    readonly_fields = ('created_at',)


@admin.register(BroadcastNotice)
class BroadcastNoticeAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'target_audience',
        'status',
        'sent_count',
        'total_recipients',
        'created_by',
        'school',
        'created_at',
    )
    list_filter = ('status', 'target_audience', 'school')
    search_fields = ('title', 'message')
    autocomplete_fields = ('school', 'created_by')
    readonly_fields = (
        'total_recipients',
        'sent_count',
        'status',
        'created_at',
    )
