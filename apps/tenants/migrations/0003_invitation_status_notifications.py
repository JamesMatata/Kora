import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def forwards_status(apps, schema_editor):
    StaffInvitation = apps.get_model('tenants', 'StaffInvitation')
    for invite in StaffInvitation.objects.all().iterator():
        invite.status = 'accepted' if invite.accepted else 'pending'
        invite.save(update_fields=['status'])


def backwards_accepted(apps, schema_editor):
    StaffInvitation = apps.get_model('tenants', 'StaffInvitation')
    for invite in StaffInvitation.objects.all().iterator():
        invite.accepted = invite.status == 'accepted'
        invite.save(update_fields=['accepted'])


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('tenants', '0002_school_memberships'),
    ]

    operations = [
        migrations.AddField(
            model_name='staffinvitation',
            name='invited_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='sent_invitations',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='staffinvitation',
            name='responded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='staffinvitation',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('accepted', 'Accepted'),
                    ('rejected', 'Rejected'),
                ],
                default='pending',
                max_length=16,
            ),
        ),
        migrations.RunPython(forwards_status, backwards_accepted),
        migrations.RemoveConstraint(
            model_name='staffinvitation',
            name='tenants_unique_pending_invitation_per_school_email',
        ),
        migrations.RemoveField(
            model_name='staffinvitation',
            name='accepted',
        ),
        migrations.AddConstraint(
            model_name='staffinvitation',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status', 'pending')),
                fields=('email', 'school'),
                name='tenants_unique_pending_invitation_per_school_email',
            ),
        ),
        migrations.CreateModel(
            name='Notification',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    'kind',
                    models.CharField(
                        choices=[
                            ('invitation', 'Invitation'),
                            ('invitation_response', 'Invitation response'),
                            ('notice', 'Notice'),
                        ],
                        default='notice',
                        max_length=32,
                    ),
                ),
                ('title', models.CharField(max_length=200)),
                ('body', models.TextField(blank=True)),
                ('link', models.CharField(blank=True, max_length=255)),
                ('is_read', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'school',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='notifications',
                        to='tenants.school',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='notifications',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
