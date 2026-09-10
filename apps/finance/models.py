from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum

from tenants.models import TenantAwareModel


class FeeStructure(TenantAwareModel):
    """Named charge that can be scoped to a grade, stream, or whole school."""

    name = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    grade_level = models.ForeignKey(
        'academics.GradeLevel',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='fee_structures',
    )
    stream = models.ForeignKey(
        'academics.ClassStream',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='fee_structures',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_fee_structures',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', 'name']

    def __str__(self):
        return f'{self.name} ({self.amount})'


class StudentFee(TenantAwareModel):
    """Amount owed by a student for a fee structure."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PARTIAL = 'partial', 'Partial'
        PAID = 'paid', 'Paid'
        WAIVED = 'waived', 'Waived'

    student = models.ForeignKey(
        'academics.Student',
        on_delete=models.CASCADE,
        related_name='fees',
    )
    fee_structure = models.ForeignKey(
        FeeStructure,
        on_delete=models.PROTECT,
        related_name='student_fees',
    )
    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'student', 'fee_structure'],
                name='finance_unique_student_fee_per_structure',
            ),
        ]

    def __str__(self):
        return f'{self.student} · {self.fee_structure.name}'

    @property
    def amount_paid(self) -> Decimal:
        total = self.payments.aggregate(total=Sum('amount'))['total']
        return total or Decimal('0.00')

    @property
    def balance(self) -> Decimal:
        if self.status == self.Status.WAIVED:
            return Decimal('0.00')
        return self.amount_due - self.amount_paid

    def refresh_status(self, save=True):
        if self.status == self.Status.WAIVED:
            return
        paid = self.amount_paid
        if paid <= 0:
            self.status = self.Status.PENDING
        elif paid >= self.amount_due:
            self.status = self.Status.PAID
        else:
            self.status = self.Status.PARTIAL
        if save:
            self.save(update_fields=['status', 'updated_at'])


class Payment(TenantAwareModel):
    """Manual payment against a student fee (M-Pesa later)."""

    class Method(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        MPESA = 'mpesa', 'M-Pesa'
        BANK = 'bank', 'Bank'

    student_fee = models.ForeignKey(
        StudentFee,
        on_delete=models.CASCADE,
        related_name='payments',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(
        max_length=16,
        choices=Method.choices,
        default=Method.MANUAL,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='recorded_payments',
    )
    note = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-paid_at', '-created_at']

    def __str__(self):
        return f'{self.amount} · {self.student_fee}'

    def save(self, *args, **kwargs):
        if self.student_fee_id and not self.school_id:
            self.school_id = self.student_fee.school_id
        super().save(*args, **kwargs)
        self.student_fee.refresh_status(save=True)
