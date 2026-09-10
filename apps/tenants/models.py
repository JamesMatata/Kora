import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from tenants.managers import TenantAwareManager, UserManager


class School(models.Model):
    """Isolated tenant representing a single school."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    code = models.SlugField(
        max_length=64,
        unique=True,
        help_text='Unique slug used for subdomain / tenant identification.',
    )
    contact_phone = models.CharField(max_length=32, blank=True)
    contact_email = models.EmailField(blank=True)
    paybill_number = models.CharField(max_length=32, blank=True)
    twilio_phone_number = models.CharField(
        max_length=16,
        blank=True,
        help_text='Optional WhatsApp sender override (E.164). Falls back to TWILIO_WHATSAPP_NUMBER.',
    )
    # Encrypted at rest (Fernet). Empty → fall back to project sandbox env keys.
    mpesa_consumer_key = models.TextField(blank=True)
    mpesa_consumer_secret = models.TextField(blank=True)
    mpesa_passkey = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def set_mpesa_credentials(self, *, consumer_key='', consumer_secret='', passkey=''):
        from tenants.crypto import encrypt_value

        if consumer_key:
            self.mpesa_consumer_key = encrypt_value(consumer_key)
        if consumer_secret:
            self.mpesa_consumer_secret = encrypt_value(consumer_secret)
        if passkey:
            self.mpesa_passkey = encrypt_value(passkey)

    def get_mpesa_credentials(self) -> dict[str, str]:
        from tenants.crypto import decrypt_value

        return {
            'consumer_key': decrypt_value(self.mpesa_consumer_key),
            'consumer_secret': decrypt_value(self.mpesa_consumer_secret),
            'passkey': decrypt_value(self.mpesa_passkey),
            'paybill_number': (self.paybill_number or '').strip(),
        }


class User(AbstractUser):
    """Global identity authenticated by email; school roles live on memberships."""

    username = None
    email = models.EmailField('email address', unique=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        ordering = ['email']

    def __str__(self):
        return self.email

    def membership_for(self, school):
        school_id = getattr(school, 'pk', school)
        return self.memberships.filter(school_id=school_id).first()


class SchoolMembership(models.Model):
    """Per-school roles for a user (GitHub org-style membership)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    is_admin = models.BooleanField(
        default=False,
        help_text='Can manage school settings, invite staff, and view all classes/finance.',
    )
    is_teacher = models.BooleanField(
        default=False,
        help_text='Can manage rosters and attendance for assigned classes.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['school__name', 'user__email']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'school'],
                name='tenants_unique_membership_per_user_school',
            ),
        ]

    def __str__(self):
        return f'{self.user.email} @ {self.school.code}'

    @property
    def role_label(self):
        if self.is_admin and self.is_teacher:
            return 'Admin & Teacher'
        if self.is_admin:
            return 'Admin'
        if self.is_teacher:
            return 'Class Teacher'
        return 'Staff'

    def clean(self):
        super().clean()

        if not self.is_admin and not self.is_teacher:
            raise ValidationError('Membership must grant Admin and/or Teacher role.')

        if self.is_admin and self.school_id:
            active_admins = SchoolMembership.objects.filter(
                school_id=self.school_id,
                is_admin=True,
                user__is_active=True,
            )
            if self.pk:
                active_admins = active_admins.exclude(pk=self.pk)
            if active_admins.count() >= 2:
                raise ValidationError(
                    {
                        'is_admin': (
                            'A school may have at most 2 active users with admin role.'
                        )
                    }
                )


class StaffInvitation(models.Model):
    """Pending invitation for a staff member to join a school tenant."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='invitations',
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_invitations',
    )
    role_admin = models.BooleanField(default=False)
    role_teacher = models.BooleanField(default=False)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['email', 'school'],
                condition=models.Q(status='pending'),
                name='tenants_unique_pending_invitation_per_school_email',
            ),
        ]

    def __str__(self):
        return f'Invite {self.email} → {self.school.code}'

    @property
    def accepted(self):
        return self.status == self.Status.ACCEPTED

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING and not self.is_expired

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    def clean(self):
        super().clean()
        if not self.role_admin and not self.role_teacher:
            raise ValidationError('Invitation must grant at least one role.')

        if self.role_admin and self.school_id and self.status == self.Status.PENDING:
            active_admins = SchoolMembership.objects.filter(
                school_id=self.school_id,
                is_admin=True,
                user__is_active=True,
            ).count()
            pending_admin_invites = StaffInvitation.objects.filter(
                school_id=self.school_id,
                role_admin=True,
                status=self.Status.PENDING,
                expires_at__gt=timezone.now(),
            )
            if self.pk:
                pending_admin_invites = pending_admin_invites.exclude(pk=self.pk)
            if active_admins + pending_admin_invites.count() >= 2:
                raise ValidationError(
                    {
                        'role_admin': (
                            'This school already has 2 active admins or pending '
                            'admin invitations.'
                        )
                    }
                )


class Notification(models.Model):
    """User inbox item (invites, invitation responses, etc.)."""

    class Kind(models.TextChoices):
        INVITATION = 'invitation', 'Invitation'
        INVITATION_RESPONSE = 'invitation_response', 'Invitation response'
        NOTICE = 'notice', 'Notice'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='notifications',
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.NOTICE)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.email}: {self.title}'


class TenantAwareModel(models.Model):
    """Abstract base for all tenant-scoped domain models."""

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='%(app_label)s_%(class)s_set',
    )

    objects = TenantAwareManager()

    class Meta:
        abstract = True
