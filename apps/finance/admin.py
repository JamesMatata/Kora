from django.contrib import admin

from finance.models import (
    FeeCategory,
    FeeInvoice,
    FeeStructure,
    LedgerEntry,
    Payment,
    PaymentPromise,
    PaymentTransaction,
    StudentFee,
    TermFeeLineItem,
    TermFeePlan,
)


@admin.register(FeeStructure)
class FeeStructureAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'amount',
        'due_date',
        'grade_level',
        'stream',
        'is_active',
        'created_by',
        'school',
    )
    list_filter = ('is_active', 'school', 'due_date')
    search_fields = ('name', 'description')
    autocomplete_fields = ('grade_level', 'stream', 'created_by', 'school')


@admin.register(StudentFee)
class StudentFeeAdmin(admin.ModelAdmin):
    list_display = (
        'student',
        'fee_structure',
        'amount_due',
        'status',
        'school',
    )
    list_filter = ('status', 'school')
    search_fields = (
        'student__admission_number',
        'student__first_name',
        'student__last_name',
        'fee_structure__name',
    )
    autocomplete_fields = ('student', 'fee_structure', 'school')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        'amount',
        'method',
        'student_fee',
        'paid_at',
        'recorded_by',
        'school',
    )
    list_filter = ('method', 'school')
    search_fields = (
        'student_fee__student__admission_number',
        'note',
    )
    autocomplete_fields = ('student_fee', 'recorded_by', 'school')


@admin.register(FeeCategory)
class FeeCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'school', 'created_at')
    list_filter = ('school',)
    search_fields = ('name', 'description')
    autocomplete_fields = ('school',)


class PaymentTransactionInline(admin.TabularInline):
    model = PaymentTransaction
    extra = 0
    fields = (
        'amount',
        'phone_number',
        'status',
        'mpesa_receipt_number',
        'created_at',
    )
    readonly_fields = ('created_at',)
    show_change_link = True


class PaymentPromiseInline(admin.TabularInline):
    model = PaymentPromise
    extra = 0
    fields = ('promised_amount', 'promised_date', 'status', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(FeeInvoice)
class FeeInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        'student',
        'term',
        'total_amount',
        'paid_amount',
        'due_date',
        'status',
        'school',
    )
    list_filter = ('status', 'school', 'term')
    search_fields = (
        'student__admission_number',
        'student__first_name',
        'student__last_name',
        'term',
    )
    autocomplete_fields = ('student', 'school')
    inlines = (PaymentTransactionInline, PaymentPromiseInline)
    readonly_fields = ('created_at', 'updated_at', 'last_contacted_at')


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = (
        'amount',
        'status',
        'phone_number',
        'invoice',
        'mpesa_receipt_number',
        'checkout_request_id',
        'created_at',
        'school',
    )
    list_filter = ('status', 'school')
    search_fields = (
        'phone_number',
        'merchant_request_id',
        'checkout_request_id',
        'mpesa_receipt_number',
        'invoice__student__admission_number',
    )
    autocomplete_fields = ('invoice', 'school')
    readonly_fields = ('created_at', 'updated_at', 'raw_callback_payload')


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        'amount',
        'invoice',
        'mpesa_receipt_number',
        'phone_number',
        'recorded_at',
        'school',
    )
    list_filter = ('school',)
    search_fields = (
        'mpesa_receipt_number',
        'phone_number',
        'invoice__student__admission_number',
    )
    autocomplete_fields = ('payment_transaction', 'invoice', 'school')
    readonly_fields = (
        'payment_transaction',
        'invoice',
        'amount',
        'phone_number',
        'mpesa_receipt_number',
        'recorded_at',
        'school',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentPromise)
class PaymentPromiseAdmin(admin.ModelAdmin):
    list_display = (
        'promised_amount',
        'promised_date',
        'status',
        'invoice',
        'school',
        'created_at',
    )
    list_filter = ('status', 'school')
    search_fields = ('invoice__student__admission_number',)
    autocomplete_fields = ('invoice', 'school')
    readonly_fields = ('created_at',)


class TermFeeLineItemInline(admin.TabularInline):
    model = TermFeeLineItem
    extra = 0
    autocomplete_fields = ('category', 'school')


@admin.register(TermFeePlan)
class TermFeePlanAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'term',
        'grade_level',
        'due_date',
        'is_active',
        'school',
        'created_at',
    )
    list_filter = ('is_active', 'school', 'term')
    search_fields = ('name', 'term')
    autocomplete_fields = ('grade_level', 'created_by', 'school')
    inlines = (TermFeeLineItemInline,)
