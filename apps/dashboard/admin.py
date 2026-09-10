from django.contrib import admin

from dashboard.models import ExecutiveWeeklyReport


@admin.register(ExecutiveWeeklyReport)
class ExecutiveWeeklyReportAdmin(admin.ModelAdmin):
    list_display = (
        'school',
        'year',
        'week_number',
        'total_billed',
        'total_collected',
        'collection_efficiency',
        'escalated_count',
        'created_at',
    )
    list_filter = ('year', 'school')
    search_fields = ('summary_markdown', 'school__name', 'school__code')
    readonly_fields = ('created_at',)
    autocomplete_fields = ('school',)
