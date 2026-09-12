from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils.text import slugify

from tenants.models import TenantAwareModel

E164_PHONE_VALIDATOR = RegexValidator(
    regex=r'^\+[1-9]\d{1,14}$',
    message='Enter a valid E.164 phone number (e.g. +254712345678).',
)


class AcademicYear(TenantAwareModel):
    """School academic year / session (e.g. 2026)."""

    name = models.CharField(max_length=32)
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_current', '-name']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'name'],
                name='academics_unique_academic_year_per_school',
            ),
            models.UniqueConstraint(
                fields=['school'],
                condition=models.Q(is_current=True),
                name='academics_unique_current_academic_year_per_school',
            ),
        ]

    def __str__(self):
        suffix = ' (current)' if self.is_current else ''
        return f'{self.name}{suffix}'


class GradeLevel(TenantAwareModel):
    """Grade / form level within a school (e.g. Grade 4, Form 1)."""

    name = models.CharField(max_length=64)
    slug = models.SlugField(
        max_length=120,
        help_text='URL-safe identifier unique within the school (e.g. grade-6).',
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text='Sort order within the school (lower first).',
    )
    class_teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_grades',
        help_text='Default teacher copied onto the class Main stream.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'name'],
                name='academics_unique_grade_level_name_per_school',
            ),
            models.UniqueConstraint(
                fields=['school', 'slug'],
                name='academics_unique_grade_level_slug_per_school',
            ),
        ]

    def __str__(self):
        return self.name

    def build_slug_base(self):
        return (slugify(self.name) or 'grade')[:100]

    def ensure_unique_slug(self):
        base = self.build_slug_base()
        candidate = base
        suffix = 2
        qs = GradeLevel.objects.filter(school_id=self.school_id, slug=candidate)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        while qs.exists():
            candidate = f'{base}-{suffix}'
            qs = GradeLevel.objects.filter(school_id=self.school_id, slug=candidate)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            suffix += 1
        self.slug = candidate

    def ensure_default_stream(self):
        """Every class has at least one stream named Main (is_default)."""
        existing_default = self.streams.filter(is_default=True).first()
        if existing_default is not None:
            return existing_default

        first = self.streams.order_by('name').first()
        if first is not None:
            first.is_default = True
            first.save(update_fields=['is_default'])
            return first

        stream = ClassStream(
            school_id=self.school_id,
            grade_level=self,
            name='Main',
            is_default=True,
            class_teacher_id=self.class_teacher_id,
        )
        stream.ensure_unique_slug()
        stream.full_clean()
        stream.save()
        return stream

    @property
    def default_stream(self):
        stream = self.streams.filter(is_default=True).first()
        if stream is not None:
            return stream
        return self.ensure_default_stream()

    def save(self, *args, **kwargs):
        creating = self._state.adding
        if not self.slug:
            self.ensure_unique_slug()
        super().save(*args, **kwargs)
        if creating:
            self.ensure_default_stream()

    def clean(self):
        super().clean()
        if not self.slug and self.name:
            self.ensure_unique_slug()
        if self.class_teacher_id:
            from tenants.models import SchoolMembership

            is_school_teacher = SchoolMembership.objects.filter(
                user_id=self.class_teacher_id,
                school_id=self.school_id,
                is_teacher=True,
                user__is_active=True,
            ).exists()
            if not is_school_teacher:
                raise ValidationError(
                    {
                        'class_teacher': (
                            'Assigned user must be an active teacher member of this school.'
                        )
                    }
                )


class ClassStream(TenantAwareModel):
    """Stream within a grade (e.g. Main, A, East), with one class teacher."""

    name = models.CharField(max_length=64)
    slug = models.SlugField(
        max_length=120,
        help_text='URL-safe identifier unique within the school (e.g. grade-6-a).',
    )
    grade_level = models.ForeignKey(
        GradeLevel,
        on_delete=models.CASCADE,
        related_name='streams',
    )
    class_teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_streams',
    )
    is_default = models.BooleanField(
        default=False,
        help_text='Default stream auto-created with every class (usually Main).',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['grade_level__order', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'grade_level', 'name'],
                name='academics_unique_stream_name_per_grade_school',
            ),
            models.UniqueConstraint(
                fields=['school', 'slug'],
                name='academics_unique_stream_slug_per_school',
            ),
            models.UniqueConstraint(
                fields=['grade_level'],
                condition=models.Q(is_default=True),
                name='academics_unique_default_stream_per_grade',
            ),
        ]

    def __str__(self):
        return f'{self.grade_level.name} {self.name}'.strip()

    def build_slug_base(self):
        grade_name = ''
        if self.grade_level_id:
            grade_name = getattr(self.grade_level, 'name', '') or ''
            if not grade_name:
                grade_name = (
                    GradeLevel.objects.filter(pk=self.grade_level_id)
                    .values_list('name', flat=True)
                    .first()
                    or ''
                )
        base = slugify(f'{grade_name}-{self.name}'.strip('-')) or 'stream'
        return base[:100]

    def ensure_unique_slug(self):
        base = self.build_slug_base()
        candidate = base
        suffix = 2
        qs = ClassStream.objects.filter(school_id=self.school_id, slug=candidate)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        while qs.exists():
            candidate = f'{base}-{suffix}'
            qs = ClassStream.objects.filter(school_id=self.school_id, slug=candidate)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            suffix += 1
        self.slug = candidate

    def save(self, *args, **kwargs):
        creating = self.pk is None
        if creating and not self.class_teacher_id and self.grade_level_id:
            grade_teacher_id = (
                GradeLevel.objects.filter(pk=self.grade_level_id)
                .values_list('class_teacher_id', flat=True)
                .first()
            )
            if grade_teacher_id:
                self.class_teacher_id = grade_teacher_id
        if not self.slug:
            self.ensure_unique_slug()
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()

        if not self.slug and self.grade_level_id and self.name:
            self.ensure_unique_slug()

        if self.grade_level_id and self.school_id:
            if self.grade_level.school_id != self.school_id:
                raise ValidationError(
                    {'grade_level': 'Grade level must belong to the same school.'}
                )

        if self.class_teacher_id:
            from tenants.models import SchoolMembership

            is_school_teacher = SchoolMembership.objects.filter(
                user_id=self.class_teacher_id,
                school_id=self.school_id,
                is_teacher=True,
                user__is_active=True,
            ).exists()
            if not is_school_teacher:
                raise ValidationError(
                    {
                        'class_teacher': (
                            'Assigned user must be an active teacher member of this school.'
                        )
                    }
                )


class Student(TenantAwareModel):
    """Enrolled student roster record.

    Placement is on a grade via its stream. Every class always has a default
    Main stream, so current_stream is required.
    """

    admission_number = models.CharField(max_length=64)
    first_name = models.CharField(max_length=120)
    last_name = models.CharField(max_length=120)
    grade_level = models.ForeignKey(
        GradeLevel,
        on_delete=models.PROTECT,
        related_name='students',
    )
    current_stream = models.ForeignKey(
        ClassStream,
        on_delete=models.PROTECT,
        related_name='students',
    )
    parent_name = models.CharField(max_length=255)
    parent_phone = models.CharField(
        max_length=16,
        validators=[E164_PHONE_VALIDATOR],
        help_text='E.164 format, e.g. +254712345678',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['admission_number']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'admission_number'],
                name='academics_unique_admission_number_per_school',
            ),
        ]

    def __str__(self):
        return f'{self.admission_number} — {self.first_name} {self.last_name}'

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    @property
    def placement_label(self):
        if self.current_stream_id:
            return str(self.current_stream)
        if self.grade_level_id:
            return self.grade_level.name
        return 'Unassigned'

    def save(self, *args, **kwargs):
        if self.current_stream_id:
            self.grade_level_id = self.current_stream.grade_level_id
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()

        if not self.grade_level_id and not self.current_stream_id:
            raise ValidationError('Student must belong to a class (grade).')

        if self.current_stream_id and self.school_id:
            if self.current_stream.school_id != self.school_id:
                raise ValidationError(
                    {'current_stream': 'Stream must belong to the same school.'}
                )
            if (
                self.grade_level_id
                and self.current_stream.grade_level_id != self.grade_level_id
            ):
                raise ValidationError(
                    {
                        'current_stream': (
                            'Stream must belong to the selected grade.'
                        )
                    }
                )

        if self.grade_level_id and self.school_id:
            if self.grade_level.school_id != self.school_id:
                raise ValidationError(
                    {'grade_level': 'Grade must belong to the same school.'}
                )


class AttendanceRecord(TenantAwareModel):
    """Daily attendance mark for one student in a stream."""

    class Status(models.TextChoices):
        PRESENT = 'PRESENT', 'Present'
        ABSENT = 'ABSENT', 'Absent'
        LATE = 'LATE', 'Late'
        EXCUSED = 'EXCUSED', 'Excused'

    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name='attendance_records',
    )
    stream = models.ForeignKey(
        ClassStream,
        on_delete=models.CASCADE,
        related_name='attendance_records',
    )
    date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PRESENT,
    )
    note = models.CharField(max_length=255, blank=True)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='marked_attendance',
    )
    parent_notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', 'student__admission_number']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'student', 'date'],
                name='academics_unique_attendance_per_student_day',
            ),
        ]
        indexes = [
            models.Index(fields=['school', 'stream', 'date']),
        ]

    def __str__(self):
        return f'{self.student} · {self.date} · {self.status}'

    def save(self, *args, **kwargs):
        if self.student_id and not self.school_id:
            self.school_id = self.student.school_id
        if self.student_id and not self.stream_id:
            self.stream_id = self.student.current_stream_id
        super().save(*args, **kwargs)


class WeeklyParentUpdate(TenantAwareModel):
    """Teacher-initiated weekly attendance + feedback batch for one stream."""

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SENT = 'SENT', 'Sent'

    stream = models.ForeignKey(
        ClassStream,
        on_delete=models.CASCADE,
        related_name='weekly_parent_updates',
    )
    week_start = models.DateField(
        help_text='Monday of the school week (Africa/Nairobi).',
    )
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
        related_name='weekly_parent_updates_created',
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='weekly_parent_updates_sent',
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    teacher_reminded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-week_start', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'stream', 'week_start'],
                name='academics_unique_weekly_update_per_stream_week',
            ),
        ]

    def __str__(self):
        return f'{self.stream} · week {self.week_start} · {self.status}'

    def save(self, *args, **kwargs):
        if self.stream_id and not self.school_id:
            self.school_id = self.stream.school_id
        super().save(*args, **kwargs)


class WeeklyStudentFeedback(TenantAwareModel):
    """Per-student line inside a weekly parent update."""

    update = models.ForeignKey(
        WeeklyParentUpdate,
        on_delete=models.CASCADE,
        related_name='feedback_lines',
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name='weekly_feedback',
    )
    attendance_summary = models.CharField(max_length=255, blank=True)
    teacher_note = models.TextField(blank=True)
    polished_note = models.TextField(blank=True)
    message_body = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    skipped_reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['student__admission_number']
        constraints = [
            models.UniqueConstraint(
                fields=['update', 'student'],
                name='academics_unique_weekly_feedback_per_student',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.update_id and not self.school_id:
            self.school_id = self.update.school_id
        super().save(*args, **kwargs)
