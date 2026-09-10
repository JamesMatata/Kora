from django.contrib import admin

from academics.models import AcademicYear, ClassStream, GradeLevel, Student


@admin.register(AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ('name', 'school', 'is_current')
    list_filter = ('is_current', 'school')
    search_fields = ('name', 'school__name', 'school__code')


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
