from django.core.management.base import BaseCommand

from finance.services.reminder_service import FeeReminderService
from tenants.models import School


class Command(BaseCommand):
    help = (
        'Dispatch overdue fee WhatsApp reminders for every active tenant school.'
    )

    def handle(self, *args, **options):
        service = FeeReminderService()
        schools = School.objects.filter(is_active=True).order_by('name')
        total = schools.count()
        if total == 0:
            self.stdout.write(self.style.WARNING('No active schools found.'))
            return

        self.stdout.write(f'Running fee reminders for {total} active school(s)...')
        totals = {
            'scanned': 0,
            'reminded': 0,
            'skipped_cooldown': 0,
            'skipped_escalated_or_promise': 0,
        }

        for school in schools.iterator():
            result = service.dispatch_overdue_reminders(school)
            self.stdout.write(result.as_console_line(school.name))
            totals['scanned'] += result.scanned
            totals['reminded'] += result.reminded
            totals['skipped_cooldown'] += result.skipped_cooldown
            totals['skipped_escalated_or_promise'] += (
                result.skipped_escalated_or_promise
            )

        self.stdout.write(
            self.style.SUCCESS(
                'Done. '
                f"Scanned: {totals['scanned']}, "
                f"Reminded: {totals['reminded']}, "
                f"Skipped (Cooldown): {totals['skipped_cooldown']}, "
                f"Skipped (Escalated/Promise): "
                f"{totals['skipped_escalated_or_promise']}"
            )
        )
