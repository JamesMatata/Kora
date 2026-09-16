from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0008_receipt_ops_weekly_updates'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentpromise',
            name='date_uncertain',
            field=models.BooleanField(
                default=False,
                help_text='Parent could not commit to a specific date (e.g. "not sure").',
            ),
        ),
        migrations.AlterField(
            model_name='paymentpromise',
            name='promised_date',
            field=models.DateField(
                blank=True,
                help_text='Null when the parent is unsure of a date.',
                null=True,
            ),
        ),
    ]
