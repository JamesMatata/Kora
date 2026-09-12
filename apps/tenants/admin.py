from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from tenants.models import (
    AuditEvent,
    Notification,
    OpsJobRun,
    School,
    SchoolMembership,
    StaffInvitation,
    User,
)


class SchoolMembershipInline(admin.TabularInline):
    model = SchoolMembership
    extra = 0
    autocomplete_fields = ('school',)


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'paybill_number', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'code', 'contact_email', 'paybill_number')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('name', 'code', 'is_active'),
        }),
        ('Contact', {
            'fields': (
                'contact_phone',
                'contact_email',
                'paybill_number',
                'twilio_phone_number',
            ),
        }),
        ('Daraja (encrypted at rest)', {
            'classes': ('collapse',),
            'fields': (
                'mpesa_environment',
                'mpesa_consumer_key',
                'mpesa_consumer_secret',
                'mpesa_passkey',
            ),
            'description': (
                'Store Fernet-encrypted values via School.set_mpesa_credentials(). '
                'Leaving secrets blank falls back to project DARAJA_* env keys. '
                'mpesa_environment blank uses DARAJA_ENVIRONMENT (sandbox|production).'
            ),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )
    prepopulated_fields = {'code': ('name',)}
    ordering = ('name',)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ('email',)
    list_display = (
        'email',
        'first_name',
        'last_name',
        'is_active',
        'is_staff',
        'is_superuser',
    )
    list_filter = ('is_active', 'is_staff', 'is_superuser')
    search_fields = ('email', 'first_name', 'last_name')
    inlines = (SchoolMembershipInline,)

    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name')}),
        (
            'Permissions',
            {
                'fields': (
                    'is_active',
                    'is_staff',
                    'is_superuser',
                    'groups',
                    'user_permissions',
                ),
            },
        ),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': (
                    'email',
                    'password1',
                    'password2',
                    'is_staff',
                    'is_superuser',
                ),
            },
        ),
    )

    filter_horizontal = ('groups', 'user_permissions')


@admin.register(SchoolMembership)
class SchoolMembershipAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'school',
        'is_admin',
        'is_teacher',
        'is_bursar',
        'phone_number',
        'created_at',
    )
    list_filter = ('is_admin', 'is_teacher', 'is_bursar', 'school')
    search_fields = ('user__email', 'school__name', 'school__code', 'phone_number')
    autocomplete_fields = ('user', 'school')


@admin.register(StaffInvitation)
class StaffInvitationAdmin(admin.ModelAdmin):
    list_display = (
        'email',
        'school',
        'role_admin',
        'role_teacher',
        'role_bursar',
        'status',
        'invited_by',
        'expires_at',
        'created_at',
    )
    list_filter = ('status', 'role_admin', 'role_teacher', 'role_bursar', 'school')
    search_fields = ('email', 'school__name', 'school__code')
    readonly_fields = ('token', 'created_at', 'responded_at')
    autocomplete_fields = ('school', 'invited_by')


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = (
        'created_at',
        'category',
        'action',
        'summary',
        'actor',
        'school',
    )
    list_filter = ('category', 'school')
    search_fields = ('summary', 'action', 'object_id')
    readonly_fields = (
        'school',
        'actor',
        'category',
        'action',
        'object_type',
        'object_id',
        'summary',
        'metadata',
        'created_at',
    )


@admin.register(OpsJobRun)
class OpsJobRunAdmin(admin.ModelAdmin):
    list_display = (
        'job_name',
        'status',
        'school',
        'summary',
        'finished_at',
    )
    list_filter = ('job_name', 'status', 'school')
    search_fields = ('job_name', 'summary', 'detail')
    readonly_fields = (
        'job_name',
        'school',
        'status',
        'summary',
        'detail',
        'started_at',
        'finished_at',
    )


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'kind', 'school', 'is_read', 'created_at')
    list_filter = ('kind', 'is_read', 'school')
    search_fields = ('title', 'body', 'user__email')
    autocomplete_fields = ('user', 'school')
