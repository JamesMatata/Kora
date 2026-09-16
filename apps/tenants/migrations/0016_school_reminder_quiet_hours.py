from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0015_membership_onboarding_dismissed'),
    ]

    operations = [
        migrations.AddField(
            model_name='school',
            name='reminder_quiet_hour_end',
            field=models.PositiveSmallIntegerField(
                default=8,
                help_text='Local hour (0–23, Africa/Nairobi) when fee-reminder quiet time ends.',
            ),
        ),
        migrations.AddField(
            model_name='school',
            name='reminder_quiet_hour_start',
            field=models.PositiveSmallIntegerField(
                default=20,
                help_text='Local hour (0–23, Africa/Nairobi) when fee-reminder quiet time begins.',
            ),
        ),
    ]
