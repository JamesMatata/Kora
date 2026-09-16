from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('communications', '0004_parentcontact_reminders_paused'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversationsession',
            name='admission_confirmed_at',
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    'When the parent confirmed the active student admission number '
                    'on this session (extra gate after phone match).'
                ),
                null=True,
            ),
        ),
    ]
