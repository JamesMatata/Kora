# Generated manually — backfill existing schools as approved + trial.

from datetime import timedelta

from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    School = apps.get_model('tenants', 'School')
    SchoolSubscription = apps.get_model('tenants', 'SchoolSubscription')
    PlatformBillingSettings = apps.get_model('tenants', 'PlatformBillingSettings')

    PlatformBillingSettings.objects.get_or_create(pk=1)
    now = timezone.now()
    for school in School.objects.all():
        School.objects.filter(pk=school.pk).update(
            status='approved',
            submitted_at=school.created_at or now,
            reviewed_at=now,
        )
        SchoolSubscription.objects.get_or_create(
            school_id=school.pk,
            defaults={
                'billing_state': 'trial',
                'trial_started_at': now,
                'trial_ends_at': now + timedelta(days=30),
            },
        )


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0011_school_verification_and_billing'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
