from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from tenants.models import TenantAwareModel

E164_PHONE_VALIDATOR = RegexValidator(
    regex=r'^\+[1-9]\d{6,14}$',
    message='Enter a valid E.164 phone number, e.g. +254712345678.',
)


class ParentContact(TenantAwareModel):
    """Guardian reachable by phone (WhatsApp / SMS), linked to one or more students."""

    phone_number = models.CharField(
        max_length=16,
        validators=[E164_PHONE_VALIDATOR],
        db_index=True,
        help_text='E.164 format, e.g. +254712345678',
    )
    parent_name = models.CharField(max_length=255, blank=True)
    students = models.ManyToManyField(
        'academics.Student',
        related_name='parent_contacts',
        blank=True,
    )
    is_verified = models.BooleanField(default=False)
    last_contacted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When this parent was last contacted by an outbound bot reminder.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['phone_number']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'phone_number'],
                name='communications_unique_parent_phone_per_school',
            ),
        ]

    def __str__(self):
        label = self.parent_name or self.phone_number
        return f'{label} ({self.phone_number})'


class ConversationSession(TenantAwareModel):
    """Ongoing chat thread with a parent contact."""

    class Status(models.TextChoices):
        BOT_ACTIVE = 'BOT_ACTIVE', 'Bot active'
        ESCALATED_PENDING = 'ESCALATED_PENDING', 'Escalated pending'
        STAFF_ACTIVE = 'STAFF_ACTIVE', 'Staff active'
        CLOSED = 'CLOSED', 'Closed'

    parent_contact = models.ForeignKey(
        ParentContact,
        on_delete=models.CASCADE,
        related_name='sessions',
    )
    active_student = models.ForeignKey(
        'academics.Student',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='conversation_sessions',
        help_text='Locks context when a parent has multiple children.',
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.BOT_ACTIVE,
    )
    assigned_staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_conversations',
    )
    last_message_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_message_at', '-created_at']
        indexes = [
            models.Index(fields=['school', 'status']),
        ]

    def __str__(self):
        return f'{self.parent_contact} · {self.status}'

    def save(self, *args, **kwargs):
        if self.parent_contact_id and not self.school_id:
            self.school_id = self.parent_contact.school_id
        super().save(*args, **kwargs)


class MessageLog(TenantAwareModel):
    """Single inbound/outbound message within a conversation session."""

    class Direction(models.TextChoices):
        INBOUND = 'INBOUND', 'Inbound'
        OUTBOUND = 'OUTBOUND', 'Outbound'

    class Sender(models.TextChoices):
        PARENT = 'PARENT', 'Parent'
        BOT = 'BOT', 'Bot'
        STAFF = 'STAFF', 'Staff'

    class DeliveryStatus(models.TextChoices):
        QUEUED = 'QUEUED', 'Queued'
        SENT = 'SENT', 'Sent'
        DELIVERED = 'DELIVERED', 'Delivered'
        READ = 'READ', 'Read'
        FAILED = 'FAILED', 'Failed'

    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    direction = models.CharField(max_length=16, choices=Direction.choices)
    sender = models.CharField(max_length=16, choices=Sender.choices)
    body = models.TextField()
    twilio_message_sid = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
    )
    delivery_status = models.CharField(
        max_length=16,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.QUEUED,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['school', 'created_at']),
        ]

    def __str__(self):
        preview = (self.body or '')[:40]
        return f'{self.direction} · {self.sender}: {preview}'

    def save(self, *args, **kwargs):
        if self.session_id and not self.school_id:
            self.school_id = self.session.school_id
        super().save(*args, **kwargs)
        from django.utils import timezone

        ConversationSession.objects.filter(pk=self.session_id).update(
            last_message_at=timezone.now(),
        )


class BroadcastNotice(TenantAwareModel):
    """School-wide WhatsApp announcement to parent phones."""

    class TargetAudience(models.TextChoices):
        ALL_PARENTS = 'ALL_PARENTS', 'All parents'
        GRADE_LEVEL = 'GRADE_LEVEL', 'Grade level'
        CLASS_STREAM = 'CLASS_STREAM', 'Class stream'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SENDING = 'SENDING', 'Sending'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    title = models.CharField(max_length=200)
    message = models.TextField()
    target_audience = models.CharField(
        max_length=32,
        choices=TargetAudience.choices,
        default=TargetAudience.ALL_PARENTS,
    )
    target_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text='GradeLevel or ClassStream primary key when audience is scoped.',
    )
    total_recipients = models.IntegerField(default=0)
    sent_count = models.IntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='broadcast_notices',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['school', 'status']),
            models.Index(fields=['school', '-created_at']),
        ]

    def __str__(self):
        return f'{self.title} · {self.status}'

    def target_label(self) -> str:
        if self.target_audience == self.TargetAudience.ALL_PARENTS:
            return 'All parents'
        if self.target_audience == self.TargetAudience.GRADE_LEVEL and self.target_id:
            from academics.models import GradeLevel

            grade = GradeLevel.objects.filter(
                school_id=self.school_id,
                pk=self.target_id,
            ).first()
            return f'Grade · {grade.name}' if grade else f'Grade #{self.target_id}'
        if self.target_audience == self.TargetAudience.CLASS_STREAM and self.target_id:
            from academics.models import ClassStream

            stream = (
                ClassStream.objects.filter(
                    school_id=self.school_id,
                    pk=self.target_id,
                )
                .select_related('grade_level')
                .first()
            )
            return str(stream) if stream else f'Stream #{self.target_id}'
        return self.get_target_audience_display()

