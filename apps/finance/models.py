from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from tenants.models import TenantAwareModel

E164_PHONE_VALIDATOR = RegexValidator(
    regex=r'^\+[1-9]\d{6,14}$',
    message='Enter a valid E.164 phone number, e.g. +254712345678.',
)


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


class FeeCategory(TenantAwareModel):
    """Named fee type within a school (Tuition, Transport, etc.)."""

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'fee categories'
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'name'],
                name='finance_unique_fee_category_per_school',
            ),
        ]

    def __str__(self):
        return self.name


class FeeInvoice(TenantAwareModel):
    """Invoice for a student for a given term."""

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PARTIALLY_PAID = 'PARTIALLY_PAID', 'Partially paid'
        PAID = 'PAID', 'Paid'
        OVERDUE = 'OVERDUE', 'Overdue'

    student = models.ForeignKey(
        'academics.Student',
        on_delete=models.CASCADE,
        related_name='invoices',
    )
    term = models.CharField(max_length=64, help_text='e.g. Term 2 2026')
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
    )
    due_date = models.DateField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    last_contacted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the guardian was last messaged about this invoice.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-due_date', '-created_at']
        indexes = [
            models.Index(fields=['school', 'status']),
            models.Index(fields=['school', 'due_date']),
        ]

    def __str__(self):
        return f'{self.student} · {self.term} · {self.total_amount}'

    def save(self, *args, **kwargs):
        if self.student_id and not self.school_id:
            self.school_id = self.student.school_id
        super().save(*args, **kwargs)

    @property
    def balance(self) -> Decimal:
        return self.total_amount - self.paid_amount

    def refresh_status(self, save=True):
        today = timezone.localdate()
        if self.paid_amount <= 0:
            self.status = (
                self.Status.OVERDUE
                if self.due_date < today
                else self.Status.PENDING
            )
        elif self.paid_amount >= self.total_amount:
            self.status = self.Status.PAID
        else:
            self.status = (
                self.Status.OVERDUE
                if self.due_date < today
                else self.Status.PARTIALLY_PAID
            )
        if save:
            self.save(update_fields=['status', 'updated_at'])

    def apply_successful_payment(self, amount: Decimal, save=True):
        self.paid_amount = (self.paid_amount or Decimal('0.00')) + amount
        self.refresh_status(save=False)
        if save:
            self.save(update_fields=['paid_amount', 'status', 'updated_at'])


class PaymentTransaction(TenantAwareModel):
    """M-Pesa (or similar) payment attempt against a fee invoice."""

    class Status(models.TextChoices):
        INITIALIZED = 'INITIALIZED', 'Initialized'
        STK_PUSH_SENT = 'STK_PUSH_SENT', 'STK push sent'
        SUCCESS = 'SUCCESS', 'Success'
        FAILED_USER = 'FAILED_USER', 'Failed (user)'
        FAILED_TIMEOUT = 'FAILED_TIMEOUT', 'Failed (timeout)'
        FAILED_ERROR = 'FAILED_ERROR', 'Failed (error)'

    TERMINAL_STATUSES = frozenset(
        {
            Status.SUCCESS,
            Status.FAILED_USER,
            Status.FAILED_TIMEOUT,
            Status.FAILED_ERROR,
        }
    )

    invoice = models.ForeignKey(
        FeeInvoice,
        on_delete=models.CASCADE,
        related_name='transactions',
    )
    phone_number = models.CharField(
        max_length=16,
        validators=[E164_PHONE_VALIDATOR],
        help_text='E.164 format, e.g. +254712345678',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    merchant_request_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )
    checkout_request_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
    )
    mpesa_receipt_number = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.INITIALIZED,
    )
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.TextField(null=True, blank=True)
    raw_callback_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['school', 'status']),
        ]

    def __str__(self):
        return f'{self.amount} · {self.status} · {self.invoice_id}'

    def save(self, *args, **kwargs):
        if self.invoice_id and not self.school_id:
            self.school_id = self.invoice.school_id

        previous_status = None
        if self.pk:
            previous_status = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list('status', flat=True)
                .first()
            )

        with transaction.atomic():
            super().save(*args, **kwargs)
            # Ledger append-only row on first SUCCESS. Invoice balance is updated
            # exclusively by reconciliation (callback) to avoid double application.
            if (
                self.status == self.Status.SUCCESS
                and previous_status != self.Status.SUCCESS
            ):
                LedgerEntry.create_from_transaction(self)


class LedgerEntry(TenantAwareModel):
    """Append-only financial ledger row created when a payment succeeds."""

    payment_transaction = models.OneToOneField(
        PaymentTransaction,
        on_delete=models.PROTECT,
        related_name='ledger_entry',
    )
    invoice = models.ForeignKey(
        FeeInvoice,
        on_delete=models.PROTECT,
        related_name='ledger_entries',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone_number = models.CharField(max_length=16)
    mpesa_receipt_number = models.CharField(max_length=64, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-recorded_at']
        verbose_name_plural = 'ledger entries'

    def __str__(self):
        return f'Ledger {self.amount} · invoice {self.invoice_id}'

    @classmethod
    def create_from_transaction(cls, payment_tx: PaymentTransaction):
        return cls.objects.get_or_create(
            payment_transaction=payment_tx,
            defaults={
                'school_id': payment_tx.school_id,
                'invoice_id': payment_tx.invoice_id,
                'amount': payment_tx.amount,
                'phone_number': payment_tx.phone_number,
                'mpesa_receipt_number': payment_tx.mpesa_receipt_number or '',
            },
        )[0]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Ledger entries are immutable and cannot be updated.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Ledger entries are immutable and cannot be deleted.')


class PaymentPromise(TenantAwareModel):
    """Commitment by a guardian/school to pay an invoice by a given date."""

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        HONORED = 'HONORED', 'Honored'
        BROKEN = 'BROKEN', 'Broken'

    invoice = models.ForeignKey(
        FeeInvoice,
        on_delete=models.CASCADE,
        related_name='promises',
    )
    promised_amount = models.DecimalField(max_digits=12, decimal_places=2)
    promised_date = models.DateField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['promised_date', '-created_at']

    def __str__(self):
        return f'{self.promised_amount} by {self.promised_date} · {self.status}'

    def save(self, *args, **kwargs):
        if self.invoice_id and not self.school_id:
            self.school_id = self.invoice.school_id
        super().save(*args, **kwargs)
