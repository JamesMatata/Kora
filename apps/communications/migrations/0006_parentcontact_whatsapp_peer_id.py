from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('communications', '0005_conversation_admission_confirmed'),
    ]

    operations = [
        migrations.AddField(
            model_name='parentcontact',
            name='whatsapp_peer_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text=(
                    'Optional Twilio WhatsApp Sandbox peer id (e.g. KE.2117397715508204). '
                    'When set, outbound WhatsApp uses this instead of phone_number.'
                ),
                max_length=64,
            ),
        ),
    ]
