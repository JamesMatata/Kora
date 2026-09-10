from django.core.management.base import BaseCommand

from dashboard.services.analytics import generate_weekly_report
from tenants.models import School


class Command(BaseCommand):
    help = 'Generate Gemini executive weekly briefings for all active schools.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Regenerate and overwrite an existing report for the current ISO week.',
        )
        parser.add_argument(
            '--school',
            type=str,
            default='',
            help='Optional school code to limit generation to one tenant.',
        )

    def handle(self, *args, **options):
        force = bool(options.get('force'))
        school_code = (options.get('school') or '').strip()
        schools = School.objects.filter(is_active=True).order_by('name')
        if school_code:
            schools = schools.filter(code__iexact=school_code)

        total = schools.count()
        if total == 0:
            self.stdout.write(self.style.WARNING('No matching active schools.'))
            return

        self.stdout.write(
            f'Generating weekly executive reports for {total} school(s)...'
        )
        counts = {'created': 0, 'updated': 0, 'exists': 0}
        for school in schools.iterator():
            report, status = generate_weekly_report(school, force=force)
            counts[status] = counts.get(status, 0) + 1
            self.stdout.write(
                f'[Tenant: {school.name}] '
                f'W{report.week_number} {report.year} - {status} - '
                f'efficiency {report.collection_efficiency}% - '
                f'conversations {report.total_conversations}'
            )

        self.stdout.write(
            self.style.SUCCESS(
                'Done. '
                f"Created: {counts['created']}, "
                f"Updated: {counts['updated']}, "
                f"Skipped existing: {counts['exists']}"
            )
        )
