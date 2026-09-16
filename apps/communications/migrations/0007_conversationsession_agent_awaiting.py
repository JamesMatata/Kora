from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('communications', '0006_parentcontact_whatsapp_peer_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversationsession',
            name='agent_awaiting',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'In-flight bot prompt context: empty, partial_amount, '
                    'promise_date, or menu. Digits are amounts when awaiting '
                    'partial_amount.'
                ),
                max_length=32,
            ),
        ),
    ]
