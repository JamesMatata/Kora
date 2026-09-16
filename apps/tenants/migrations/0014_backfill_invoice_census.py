# Backfill census fields on existing platform invoices.

from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    PlatformInvoice = apps.get_model('tenants', 'PlatformInvoice')
    PlatformBillingSettings = apps.get_model('tenants', 'PlatformBillingSettings')
    today = timezone.localdate()
    PlatformBillingSettings.objects.get_or_create(pk=1)
    for inv in PlatformInvoice.objects.all():
        updates = {
            'kind': 'main',
            'census_lock_date': inv.due_half_at,
        }
        if inv.due_half_at and today >= inv.due_half_at:
            updates['census_status'] = 'locked'
            if inv.census_locked_at is None:
                updates['census_locked_at'] = timezone.now()
        else:
            updates['census_status'] = 'provisional'
        PlatformInvoice.objects.filter(pk=inv.pk).update(**updates)


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0013_census_true_up_credits'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
