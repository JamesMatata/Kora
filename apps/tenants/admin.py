from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from tenants.models import (
    AuditEvent,
    Notification,
    OpsJobRun,
    PlatformBillingSettings,
    PlatformInvoice,
    School,
    SchoolApplicationDocument,
    SchoolMembership,
    SchoolSubscription,
    SchoolTermPeriod,
    StaffInvitation,
    User,
)


class SchoolMembershipInline(admin.TabularInline):
    model = SchoolMembership
    extra = 0
    autocomplete_fields = ('school',)


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'code',
        'status',
        'paybill_number',
        'is_active',
        'created_at',
    )
    list_filter = ('status', 'is_active')
    search_fields = ('name', 'code', 'contact_email', 'paybill_number')
    readonly_fields = ('created_at', 'updated_at', 'submitted_at', 'reviewed_at')
    fieldsets = (
        (None, {
            'fields': ('name', 'code', 'status', 'is_active'),
        }),
        ('Application', {
            'fields': (
                'applicant_role',
                'physical_address',
                'county',
                'estimated_student_count',
                'submitted_at',
                'reviewed_at',
                'reviewed_by',
                'rejection_reason',
            ),
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
                'daraja_webhook_secret',
            ),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )
    prepopulated_fields = {'code': ('name',)}
    ordering = ('name',)


@admin.register(SchoolApplicationDocument)
class SchoolApplicationDocumentAdmin(admin.ModelAdmin):
    list_display = ('school', 'doc_type', 'original_name', 'uploaded_at')
    list_filter = ('doc_type',)
    search_fields = ('school__name', 'school__code', 'original_name')


@admin.register(SchoolTermPeriod)
class SchoolTermPeriodAdmin(admin.ModelAdmin):
    list_display = (
        'school',
        'academic_year_label',
        'term_number',
        'start_date',
        'end_date',
    )
    list_filter = ('academic_year_label', 'term_number')


@admin.register(PlatformBillingSettings)
class PlatformBillingSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ('Tiers', {
            'fields': (
                'starter_max_students',
                'growth_max_students',
                'starter_rate',
                'growth_rate',
                'scale_rate',
                'starter_floor',
            ),
        }),
        ('Trial & payment windows', {
            'fields': (
                'trial_days',
                'post_trial_grace_days',
                'term_half_due_days',
                'term_full_due_days',
                'min_proration_factor',
                'roll_into_next_term_days',
            ),
        }),
        ('Census & true-up', {
            'fields': (
                'true_up_factor',
                'true_up_min_delta',
                'census_policy_text',
            ),
        }),
        ('Messages', {
            'fields': ('soft_lock_message', 'pay_instructions'),
        }),
    )

    def has_add_permission(self, request):
        return not PlatformBillingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SchoolSubscription)
class SchoolSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        'school',
        'billing_state',
        'credit_balance',
        'trial_ends_at',
        'locked_at',
    )
    list_filter = ('billing_state',)
    search_fields = ('school__name', 'school__code')


@admin.register(PlatformInvoice)
class PlatformInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        'school',
        'term_label',
        'kind',
        'census_status',
        'tier',
        'headcount',
        'amount_due',
        'amount_paid',
        'status',
    )
    list_filter = ('status', 'tier', 'kind', 'census_status')
    search_fields = ('school__name', 'term_label')
    readonly_fields = ('created_at', 'updated_at', 'census_locked_at', 'true_up_settled_at')


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
