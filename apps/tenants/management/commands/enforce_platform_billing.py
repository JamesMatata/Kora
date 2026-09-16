"""Create term invoices and soft-lock overdue Kora subscriptions."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from tenants.ops import record_ops_job
from tenants.platform_billing import enforce_all_schools


class Command(BaseCommand):
    help = (
        'Generate platform term invoices after trial and soft-lock schools '
        'that miss half/full payment deadlines.'
    )

    def handle(self, *args, **options):
        started = timezone.now()
        try:
            stats = enforce_all_schools()
            summary = (
                f"schools={stats['schools']} invoices+={stats['invoices']} "
                f"census_locks={stats['census_locks']} true_ups={stats['true_ups']} "
                f"locked={stats['locked']}"
            )
            self.stdout.write(self.style.SUCCESS(summary))
            record_ops_job(
                job_name='platform_billing',
                status='OK',
                summary=summary,
                started_at=started,
            )
        except Exception as exc:
            record_ops_job(
                job_name='platform_billing',
                status='FAILED',
                summary=str(exc)[:255],
                started_at=started,
            )
            raise
