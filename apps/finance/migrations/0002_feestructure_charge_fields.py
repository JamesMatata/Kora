# Generated manually for FeeStructure charge fields

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('finance', '0001_initial'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='feestructure',
            name='finance_unique_fee_name_per_school',
        ),
        migrations.AddField(
            model_name='feestructure',
            name='created_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='created_fee_structures',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='feestructure',
            name='description',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='feestructure',
            name='due_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterModelOptions(
            name='feestructure',
            options={'ordering': ['-created_at', 'name']},
        ),
    ]
