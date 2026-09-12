from django.contrib import admin

from academics.models import (
    AcademicYear,
    AttendanceRecord,
    ClassStream,
    GradeLevel,
    Student,
    WeeklyParentUpdate,
    WeeklyStudentFeedback,
)


@admin.register(AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ('name', 'school', 'is_current')
    list_filter = ('is_current', 'school')
    search_fields = ('name', 'school__name', 'school__code')


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = (
        'date',
        'student',
        'stream',
        'status',
        'parent_notified_at',
        'school',
    )
    list_filter = ('status', 'date', 'school')
    search_fields = (
        'student__admission_number',
        'student__first_name',
        'student__last_name',
    )
    autocomplete_fields = ('student', 'stream', 'marked_by', 'school')
    date_hierarchy = 'date'


@admin.register(WeeklyParentUpdate)
class WeeklyParentUpdateAdmin(admin.ModelAdmin):
    list_display = ('stream', 'week_start', 'status', 'sent_at', 'school')
    list_filter = ('status', 'school')
    search_fields = ('stream__name', 'stream__slug')
    autocomplete_fields = ('stream', 'created_by', 'sent_by', 'school')
    date_hierarchy = 'week_start'


@admin.register(WeeklyStudentFeedback)
class WeeklyStudentFeedbackAdmin(admin.ModelAdmin):
    list_display = ('student', 'update', 'sent_at', 'skipped_reason', 'school')
    list_filter = ('skipped_reason', 'school')
    search_fields = (
        'student__admission_number',
        'student__first_name',
        'student__last_name',
    )
    autocomplete_fields = ('update', 'student', 'school')

@admin.register(GradeLevel)
class GradeLevelAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'order', 'class_teacher', 'school')
    list_filter = ('school',)
    search_fields = ('name', 'slug', 'school__name')
    ordering = ('school', 'order', 'name')
    autocomplete_fields = ('class_teacher', 'school')
    readonly_fields = ('slug',)


@admin.register(ClassStream)
class ClassStreamAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'slug',
        'grade_level',
        'is_default',
        'class_teacher',
        'school',
    )
    list_filter = ('school', 'grade_level', 'is_default')
    search_fields = ('name', 'slug', 'grade_level__name', 'class_teacher__email')
    autocomplete_fields = ('grade_level', 'class_teacher', 'school')
    readonly_fields = ('slug',)


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = (
        'admission_number',
        'first_name',
        'last_name',
        'grade_level',
        'current_stream',
        'parent_phone',
        'is_active',
        'school',
    )
    list_filter = ('is_active', 'school', 'grade_level')
    search_fields = (
        'admission_number',
        'first_name',
        'last_name',
        'parent_name',
        'parent_phone',
    )
    autocomplete_fields = ('grade_level', 'current_stream', 'school')
