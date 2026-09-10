from django.contrib import admin

from finance.models import FeeStructure, Payment, StudentFee


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
