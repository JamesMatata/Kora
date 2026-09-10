from django.db import models

from tenants.models import TenantAwareModel


class ExecutiveWeeklyReport(TenantAwareModel):
    """AI-generated weekly executive briefing for a school."""

    week_number = models.PositiveSmallIntegerField()
    year = models.PositiveIntegerField()
    total_billed = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_collected = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    collection_efficiency = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=0,
        help_text='Collection rate as a percentage (0–100).',
    )
    total_conversations = models.PositiveIntegerField(default=0)
    automated_resolved_count = models.PositiveIntegerField(default=0)
    escalated_count = models.PositiveIntegerField(default=0)
    summary_markdown = models.TextField(blank=True)
    key_action_items = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-year', '-week_number', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'year', 'week_number'],
                name='dashboard_unique_weekly_report_per_school',
            ),
        ]
        indexes = [
            models.Index(fields=['school', '-year', '-week_number']),
        ]

    def __str__(self):
        return f'{self.school} · W{self.week_number} {self.year}'

    @property
    def outstanding(self):
        from decimal import Decimal

        value = self.total_billed - self.total_collected
        return value if value > 0 else Decimal('0.00')
