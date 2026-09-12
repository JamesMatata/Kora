from django.core.management.base import BaseCommand
from django.utils import timezone

from finance.services.reminder_service import FeeReminderService
from tenants.models import OpsJobRun, School
from tenants.ops import record_ops_job


class Command(BaseCommand):
    help = (
        'Dispatch overdue fee WhatsApp reminders for every active tenant school.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--school',
            dest='school_code',
            default='',
            help='Optional school code; default is all active schools.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help=(
                'Bypass quiet hours and same-day contact cap for testing. '
                'Weekly limit of 2 reminders per parent still applies.'
            ),
        )

    def handle(self, *args, **options):
        started = timezone.now()
        service = FeeReminderService()
        force = bool(options.get('force'))
        schools = School.objects.filter(is_active=True).order_by('name')
        school_code = (options.get('school_code') or '').strip()
        if school_code:
            schools = schools.filter(code__iexact=school_code)
        total = schools.count()
        if total == 0:
            self.stdout.write(self.style.WARNING('No active schools found.'))
            return

        mode = ' (force)' if force else ''
        self.stdout.write(
            f'Running fee reminders for {total} active school(s){mode}...'
        )
        totals = {
            'scanned': 0,
            'reminded': 0,
            'skipped_cooldown': 0,
            'skipped_quiet_hours': 0,
            'skipped_paused': 0,
            'skipped_escalated_or_promise': 0,
        }

        for school in schools.iterator():
            try:
                result = service.dispatch_overdue_reminders(school, force=force)
                self.stdout.write(result.as_console_line(school.name))
                totals['scanned'] += result.scanned
                totals['reminded'] += result.reminded
                totals['skipped_cooldown'] += result.skipped_cooldown
                totals['skipped_quiet_hours'] += result.skipped_quiet_hours
                totals['skipped_paused'] += result.skipped_paused
                totals['skipped_escalated_or_promise'] += (
                    result.skipped_escalated_or_promise
                )
                record_ops_job(
                    job_name='fee_reminders',
                    status=OpsJobRun.Status.OK,
                    summary=(
                        f'sent {result.reminded}, '
                        f'scanned {result.scanned}, '
                        f'paused {result.skipped_paused}'
                    ),
                    detail=result.as_console_line(school.name),
                    school=school,
                    started_at=started,
                )
            except Exception as exc:
                record_ops_job(
                    job_name='fee_reminders',
                    status=OpsJobRun.Status.FAILED,
                    summary=str(exc)[:255],
                    detail=str(exc),
                    school=school,
                    started_at=started,
                )
                raise

        self.stdout.write(
            self.style.SUCCESS(
                'Done. '
                f"Scanned: {totals['scanned']}, "
                f"Reminded: {totals['reminded']}, "
                f"Skipped (Cooldown): {totals['skipped_cooldown']}, "
                f"Skipped (Paused): {totals['skipped_paused']}, "
                f"Skipped (Quiet hours): {totals['skipped_quiet_hours']}, "
                f"Skipped (Escalated/Promise): "
                f"{totals['skipped_escalated_or_promise']}"
            )
        )
