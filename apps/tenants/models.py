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
    reminder_quiet_hour_start = models.PositiveSmallIntegerField(
        default=20,
        help_text='Local hour (0–23, Africa/Nairobi) when fee-reminder quiet time begins.',
    )
    reminder_quiet_hour_end = models.PositiveSmallIntegerField(
        default=8,
        help_text='Local hour (0–23, Africa/Nairobi) when fee-reminder quiet time ends.',
    )
    # Encrypted at rest (Fernet). Empty → fall back to project sandbox env keys.
    mpesa_consumer_key = models.TextField(blank=True)
    mpesa_consumer_secret = models.TextField(blank=True)
    mpesa_passkey = models.TextField(blank=True)
    mpesa_environment = models.CharField(
        max_length=16,
        blank=True,
        choices=(
            ('', 'Platform default'),
            ('sandbox', 'Sandbox'),
            ('production', 'Production'),
        ),
        help_text=(
            'Daraja API host. Blank uses DARAJA_ENVIRONMENT from the server. '
            'Use production only with live Safaricom credentials.'
        ),
    )
    # Encrypted at rest. Included as ?token= on Daraja callback URLs.
    daraja_webhook_secret = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Status(models.TextChoices):
        PENDING_REVIEW = 'pending_review', 'Pending review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        SUSPENDED = 'suspended', 'Suspended'

    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PENDING_REVIEW,
        db_index=True,
    )
    applicant_role = models.CharField(max_length=64, blank=True)
    physical_address = models.TextField(blank=True)
    county = models.CharField(max_length=64, blank=True)
    estimated_student_count = models.PositiveIntegerField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_schools',
    )
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def is_pending_review(self) -> bool:
        return self.status == self.Status.PENDING_REVIEW

    @property
    def is_approved(self) -> bool:
        return self.status == self.Status.APPROVED

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

    def set_daraja_webhook_token(self, token: str) -> None:
        from tenants.crypto import encrypt_value

        self.daraja_webhook_secret = encrypt_value(token) if token else ''

    def get_daraja_webhook_token(self) -> str:
        from tenants.crypto import decrypt_value

        return decrypt_value(self.daraja_webhook_secret)

    def ensure_daraja_webhook_token(self) -> str:
        """Return the plaintext webhook token, generating one if missing."""
        import secrets

        existing = self.get_daraja_webhook_token()
        if existing:
            return existing
        token = secrets.token_urlsafe(32)
        self.set_daraja_webhook_token(token)
        self.save(update_fields=['daraja_webhook_secret', 'updated_at'])
        return token


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
    is_bursar = models.BooleanField(
        default=False,
        help_text='Can manage fees, Paybill settings, invoices, and collections.',
    )
    phone_number = models.CharField(
        max_length=16,
        blank=True,
        help_text='Optional staff WhatsApp for ops reminders (E.164 preferred).',
    )
    onboarding_dismissed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the admin dismissed the Get started checklist for this school.',
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
        parts = []
        if self.is_admin:
            parts.append('Admin')
        if self.is_bursar:
            parts.append('Bursar')
        if self.is_teacher:
            parts.append('Teacher')
        return ' & '.join(parts) if parts else 'Staff'

    def clean(self):
        super().clean()

        if not self.is_admin and not self.is_teacher and not self.is_bursar:
            raise ValidationError(
                'Membership must grant Admin, Teacher, and/or Bursar role.'
            )

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
    role_bursar = models.BooleanField(default=False)
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
        if not self.role_admin and not self.role_teacher and not self.role_bursar:
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


class AuditEvent(TenantAwareModel):
    """Append-only audit trail for money and role changes."""

    class Category(models.TextChoices):
        PAYMENT = 'PAYMENT', 'Payment'
        ROLE = 'ROLE', 'Role'
        SETTINGS = 'SETTINGS', 'Settings'
        OTHER = 'OTHER', 'Other'

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_events',
    )
    category = models.CharField(
        max_length=16,
        choices=Category.choices,
        default=Category.OTHER,
    )
    action = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['school', 'category', 'created_at']),
        ]

    def __str__(self):
        return f'{self.category}: {self.summary}'


class OpsJobRun(models.Model):
    """Last-run telemetry for scheduled manage.py jobs (reminders, backups)."""

    class Status(models.TextChoices):
        OK = 'OK', 'OK'
        FAILED = 'FAILED', 'Failed'

    job_name = models.CharField(max_length=64, db_index=True)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='ops_job_runs',
        help_text='Null = platform-wide job (e.g. database backup).',
    )
    status = models.CharField(max_length=16, choices=Status.choices)
    summary = models.CharField(max_length=255, blank=True)
    detail = models.TextField(blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-finished_at']
        indexes = [
            models.Index(fields=['job_name', '-finished_at']),
        ]

    def __str__(self):
        return f'{self.job_name} · {self.status} · {self.finished_at}'


class SchoolApplicationDocument(models.Model):
    """Uploaded KYB document for a school application."""

    class DocType(models.TextChoices):
        SCHOOL_REGISTRATION = 'school_registration', 'School registration / MoE licence'
        APPLICANT_ID = 'applicant_id', 'Applicant National ID / passport'
        AUTHORITY_LETTER = 'authority_letter', 'Authority letter'
        KRA_PIN = 'kra_pin', 'KRA PIN (optional)'
        CR12 = 'cr12', 'CR12 (optional)'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='application_documents',
    )
    doc_type = models.CharField(max_length=32, choices=DocType.choices)
    file = models.FileField(upload_to='school_applications/%Y/%m/')
    original_name = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_school_documents',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['doc_type', '-uploaded_at']

    def __str__(self):
        return f'{self.school.code}: {self.get_doc_type_display()}'


class SchoolTermPeriod(models.Model):
    """School term date range used for platform billing schedules."""

    class TermNumber(models.IntegerChoices):
        TERM_1 = 1, 'Term 1'
        TERM_2 = 2, 'Term 2'
        TERM_3 = 3, 'Term 3'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='term_periods',
    )
    academic_year_label = models.CharField(max_length=32)
    term_number = models.PositiveSmallIntegerField(choices=TermNumber.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['academic_year_label', 'term_number']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'academic_year_label', 'term_number'],
                name='tenants_unique_school_term_period',
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F('start_date')),
                name='tenants_term_period_end_after_start',
            ),
        ]

    def __str__(self):
        return (
            f'{self.school.code} {self.academic_year_label} '
            f'Term {self.term_number}'
        )

    @property
    def label(self) -> str:
        return f'Term {self.term_number} {self.academic_year_label}'

    @property
    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class PlatformBillingSettings(models.Model):
    """Singleton-style platform pricing and grace configuration."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    starter_max_students = models.PositiveIntegerField(default=300)
    growth_max_students = models.PositiveIntegerField(default=750)
    starter_rate = models.DecimalField(max_digits=10, decimal_places=2, default=120)
    growth_rate = models.DecimalField(max_digits=10, decimal_places=2, default=100)
    scale_rate = models.DecimalField(max_digits=10, decimal_places=2, default=80)
    starter_floor = models.DecimalField(max_digits=12, decimal_places=2, default=20000)
    trial_days = models.PositiveIntegerField(default=30)
    post_trial_grace_days = models.PositiveIntegerField(default=14)
    term_half_due_days = models.PositiveIntegerField(default=14)
    term_full_due_days = models.PositiveIntegerField(default=30)
    min_proration_factor = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0.5,
        help_text='Minimum first-invoice proration factor (e.g. 0.5 = 50%).',
    )
    roll_into_next_term_days = models.PositiveIntegerField(
        default=14,
        help_text=(
            'If fewer than this many days remain in the current term when the '
            'trial ends, roll the first charge into the next term.'
        ),
    )
    soft_lock_message = models.TextField(
        blank=True,
        default=(
            'This school is locked until the Kora subscription payment is '
            'caught up. You can still switch to other schools.'
        ),
    )
    pay_instructions = models.TextField(
        blank=True,
        default=(
            'Pay the amount due to Kora using the instructions provided by '
            'your account manager. Your invoice will be marked paid once '
            'payment is confirmed (usually within 1 business day).'
        ),
    )
    true_up_factor = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0.5,
        help_text='Fraction of full per-student term rate for end-of-term net adjustments.',
    )
    true_up_min_delta = models.PositiveIntegerField(
        default=10,
        help_text='Ignore net student changes smaller than this at term end.',
    )
    census_policy_text = models.TextField(
        blank=True,
        default=(
            'Your main term fee is set from active students on day 14 of the term '
            '(census lock). Students who join or leave after that are settled once '
            'at term end as a net adjustment at half the per-student term rate. '
            'Growth creates a small add-on invoice. Net losses become credit on your '
            'next term bill — we do not issue cash refunds. Changes under 10 students '
            'are ignored as normal churn.'
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'platform billing settings'
        verbose_name_plural = 'platform billing settings'

    def __str__(self):
        return 'Platform billing settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> 'PlatformBillingSettings':
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class SchoolSubscription(models.Model):
    """Per-school Kora SaaS subscription state."""

    class BillingState(models.TextChoices):
        TRIAL = 'trial', 'Trial'
        OK = 'ok', 'In good standing'
        GRACE = 'grace', 'Grace period'
        LOCKED = 'locked', 'Soft-locked'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.OneToOneField(
        School,
        on_delete=models.CASCADE,
        related_name='subscription',
    )
    billing_state = models.CharField(
        max_length=16,
        choices=BillingState.choices,
        default=BillingState.TRIAL,
        db_index=True,
    )
    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_term_label = models.CharField(max_length=64, blank=True)
    credit_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text='KES credit from net mid-term student losses; applied to the next main invoice.',
    )
    locked_at = models.DateTimeField(null=True, blank=True)
    lock_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['school__name']

    def __str__(self):
        return f'{self.school.code}: {self.billing_state}'

    @property
    def is_locked(self) -> bool:
        return self.billing_state == self.BillingState.LOCKED


class PlatformInvoice(models.Model):
    """Kora platform subscription invoice for one school term."""

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        PARTIAL = 'partial', 'Partially paid'
        PAID = 'paid', 'Paid'
        VOID = 'void', 'Void'

    class Tier(models.TextChoices):
        STARTER = 'starter', 'Starter'
        GROWTH = 'growth', 'Growth'
        SCALE = 'scale', 'Scale'

    class Kind(models.TextChoices):
        MAIN = 'main', 'Main term invoice'
        TRUE_UP = 'true_up', 'End-of-term true-up'
        CREDIT_NOTE = 'credit_note', 'Credit note'

    class CensusStatus(models.TextChoices):
        PROVISIONAL = 'provisional', 'Provisional (pre day-14)'
        LOCKED = 'locked', 'Census locked'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='platform_invoices',
    )
    term_period = models.ForeignKey(
        SchoolTermPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='platform_invoices',
    )
    related_invoice = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='adjustments',
        help_text='Parent main invoice for true-ups.',
    )
    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.MAIN,
        db_index=True,
    )
    term_label = models.CharField(max_length=64)
    headcount = models.PositiveIntegerField(default=0)
    headcount_delta = models.IntegerField(
        default=0,
        help_text='For true-ups: net students vs locked census (can be negative).',
    )
    tier = models.CharField(max_length=16, choices=Tier.choices)
    rate_per_student = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    floor_applied = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    proration_factor = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=1,
    )
    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit_applied = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text='Subscription credit applied when this invoice was issued.',
    )
    due_half_at = models.DateField(null=True, blank=True)
    due_full_at = models.DateField(null=True, blank=True)
    census_status = models.CharField(
        max_length=16,
        choices=CensusStatus.choices,
        default=CensusStatus.PROVISIONAL,
    )
    census_lock_date = models.DateField(
        null=True,
        blank=True,
        help_text='Date the day-14 census locks (usually term start + 14).',
    )
    census_locked_at = models.DateTimeField(null=True, blank=True)
    true_up_settled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When end-of-term net adjustment was processed for this main invoice.',
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    is_first_billable = models.BooleanField(
        default=False,
        help_text='First invoice after trial (may be mid-term prorated).',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['school', 'status']),
            models.Index(fields=['school', 'term_label']),
            models.Index(fields=['school', 'kind', 'status']),
        ]

    def __str__(self):
        return f'{self.school.code} {self.term_label} ({self.kind}): {self.amount_due}'

    @property
    def balance(self):
        return self.amount_due - self.amount_paid

    @property
    def is_census_locked(self) -> bool:
        return self.census_status == self.CensusStatus.LOCKED

    @property
    def half_due_amount(self):
        from decimal import Decimal, ROUND_HALF_UP

        return (self.amount_due * Decimal('0.5')).quantize(
            Decimal('0.01'),
            rounding=ROUND_HALF_UP,
        )
