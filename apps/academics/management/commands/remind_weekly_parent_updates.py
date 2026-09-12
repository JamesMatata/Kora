"""Remind class teachers to send weekly parent WhatsApp updates."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from academics.services.weekly_updates import remind_teachers_weekly_updates
from tenants.models import OpsJobRun, School
from tenants.ops import record_ops_job


class Command(BaseCommand):
    help = (
        'Nudge class teachers (in-app + optional WhatsApp) to send weekly '
        'parent attendance updates. Intended for Friday 16:00 Africa/Nairobi cron.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--school',
            dest='school_code',
            default='',
            help='Optional school code; default is all active schools.',
        )

    def handle(self, *args, **options):
        started = timezone.now()
        school_code = (options.get('school_code') or '').strip()
        schools = School.objects.filter(is_active=True).order_by('name')
        if school_code:
            schools = schools.filter(code__iexact=school_code)

        total_reminded = 0
        for school in schools.iterator():
            result = remind_teachers_weekly_updates(school=school)
            reminded = int(result.get('reminded') or 0)
            total_reminded += reminded
            self.stdout.write(
                f'{school.name}: reminded={reminded} ({result.get("reason")})'
            )
            record_ops_job(
                job_name='weekly_teacher_reminders',
                status=OpsJobRun.Status.OK,
                summary=f'reminded {reminded}',
                detail=str(result),
                school=school,
                started_at=started,
            )

        self.stdout.write(
            self.style.SUCCESS(f'Done. Teachers reminded: {total_reminded}')
        )
