"""Re-encrypt School secrets with the current KORA_CREDENTIALS_KEY (or legacy)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from tenants.crypto import decrypt_value, encrypt_value
from tenants.models import School

SECRET_FIELDS = (
    'mpesa_consumer_key',
    'mpesa_consumer_secret',
    'mpesa_passkey',
    'daraja_webhook_secret',
)


class Command(BaseCommand):
    help = (
        'Decrypt and re-encrypt School credential fields with the active '
        'KORA_CREDENTIALS_KEY (falls back to SECRET_KEY-derived Fernet).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report how many fields would be rewritten without saving.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        updated_schools = 0
        updated_fields = 0

        for school in School.objects.all().iterator():
            changes: list[str] = []
            for field in SECRET_FIELDS:
                token = getattr(school, field) or ''
                if not token:
                    continue
                try:
                    plain = decrypt_value(token)
                except ValueError:
                    self.stderr.write(
                        self.style.ERROR(
                            f'Cannot decrypt {field} for school={school.id} '
                            f'({school.code}); skip field.'
                        )
                    )
                    continue
                new_token = encrypt_value(plain)
                if new_token != token:
                    setattr(school, field, new_token)
                    changes.append(field)
                    updated_fields += 1

            if not changes:
                continue
            updated_schools += 1
            if dry_run:
                self.stdout.write(
                    f'[dry-run] would update {school.code}: {", ".join(changes)}'
                )
            else:
                school.save(update_fields=[*changes, 'updated_at'])
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Updated {school.code}: {", ".join(changes)}'
                    )
                )

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(
            f'{verb} {updated_fields} field(s) across {updated_schools} school(s).'
        )
