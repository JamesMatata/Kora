"""Create a timestamped database backup (SQLite copy or pg_dump)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tenants.models import OpsJobRun
from tenants.ops import record_ops_job


class Command(BaseCommand):
    help = (
        'Write a database backup under BACKUP_DIR (default: backups/). '
        'SQLite: file copy. PostgreSQL: requires pg_dump on PATH.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dir',
            dest='backup_dir',
            default='',
            help='Override backup directory (default BACKUP_DIR or ./backups).',
        )

    def handle(self, *args, **options):
        started = timezone.now()
        backup_root = Path(
            options.get('backup_dir')
            or getattr(settings, 'BACKUP_DIR', '')
            or (Path(settings.BASE_DIR) / 'backups')
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')

        db = settings.DATABASES['default']
        engine = db.get('ENGINE', '')

        try:
            if 'sqlite' in engine:
                db_path = Path(db['NAME'])
                if not db_path.is_absolute():
                    db_path = Path(settings.BASE_DIR) / db_path
                if not db_path.exists():
                    raise CommandError(f'SQLite database not found: {db_path}')
                dest = backup_root / f'kora-sqlite-{stamp}.db'
                shutil.copy2(db_path, dest)
                size = dest.stat().st_size
                record_ops_job(
                    job_name='backup_database',
                    status=OpsJobRun.Status.OK,
                    summary=f'Wrote {dest.name} ({size} bytes)',
                    detail=str(dest),
                    started_at=started,
                )
                self.stdout.write(self.style.SUCCESS(f'Wrote {dest}'))
                return

            if 'postgresql' in engine or 'psycopg' in engine:
                import os

                dest = backup_root / f'kora-postgres-{stamp}.sql'
                env = os.environ.copy()
                if db.get('PASSWORD'):
                    env['PGPASSWORD'] = str(db['PASSWORD'])
                cmd = [
                    'pg_dump',
                    '-h',
                    str(db.get('HOST') or 'localhost'),
                    '-p',
                    str(db.get('PORT') or '5432'),
                    '-U',
                    str(db.get('USER') or 'postgres'),
                    '-d',
                    str(db.get('NAME')),
                    '-f',
                    str(dest),
                ]
                try:
                    subprocess.run(
                        cmd, check=True, env=env, capture_output=True, text=True
                    )
                except FileNotFoundError as exc:
                    raise CommandError(
                        'pg_dump not found on PATH. Install PostgreSQL client tools.'
                    ) from exc
                except subprocess.CalledProcessError as exc:
                    raise CommandError(exc.stderr or str(exc)) from exc
                size = dest.stat().st_size if dest.exists() else 0
                record_ops_job(
                    job_name='backup_database',
                    status=OpsJobRun.Status.OK,
                    summary=f'Wrote {dest.name} ({size} bytes)',
                    detail=str(dest),
                    started_at=started,
                )
                self.stdout.write(self.style.SUCCESS(f'Wrote {dest}'))
                return

            raise CommandError(f'Unsupported database engine for backup: {engine}')
        except Exception as exc:
            record_ops_job(
                job_name='backup_database',
                status=OpsJobRun.Status.FAILED,
                summary=str(exc)[:255],
                detail=str(exc),
                started_at=started,
            )
            raise
