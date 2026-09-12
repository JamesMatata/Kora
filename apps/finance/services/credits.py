"""Allocate payments across invoices and hold overpayment as student fee credit."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from finance.models import (
    FeeInvoice,
    StudentFeeCredit,
    StudentFeeCreditMovement,
)

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')


@dataclass
class AllocationResult:
    amount_received: Decimal
    applied_to_invoices: list[dict] = field(default_factory=list)
    credit_added: Decimal = ZERO
    credit_balance: Decimal = ZERO

    @property
    def has_credit(self) -> bool:
        return self.credit_added > 0


def get_or_create_credit(school, student) -> StudentFeeCredit:
    credit, _ = StudentFeeCredit.objects.get_or_create(
        student=student,
        defaults={'school': school, 'balance': ZERO},
    )
    return credit


@transaction.atomic
def apply_payment_across_invoices(
    *,
    school,
    student,
    amount: Decimal,
    payment_tx=None,
    primary_invoice=None,
) -> AllocationResult:
    """
    Waterfall payment onto open invoices (primary first, then FIFO by due_date).
    Leftover becomes student fee credit.
    """
    amount = Decimal(str(amount or '0'))
    if amount <= 0:
        return AllocationResult(amount_received=ZERO)

    remaining = amount
    applied: list[dict] = []

    open_qs = (
        FeeInvoice.objects.select_for_update()
        .filter(school=school, student=student)
        .exclude(status=FeeInvoice.Status.PAID)
        .order_by('due_date', 'created_at')
    )
    ordered: list = []
    if primary_invoice is not None:
        primary = (
            FeeInvoice.objects.select_for_update()
            .filter(pk=primary_invoice.pk, school=school, student=student)
            .first()
        )
        if primary is not None and primary.status != FeeInvoice.Status.PAID:
            ordered.append(primary)
        for inv in open_qs:
            if primary is None or inv.pk != primary.pk:
                ordered.append(inv)
    else:
        ordered = list(open_qs)

    for invoice in ordered:
        if remaining <= 0:
            break
        balance = max(invoice.balance, ZERO)
        if balance <= 0:
            continue
        take = min(remaining, balance)
        invoice.apply_successful_payment(take, save=True)
        applied.append(
            {
                'invoice_id': str(invoice.pk),
                'term': invoice.term,
                'amount': take,
            }
        )
        remaining -= take

    credit_added = ZERO
    credit_balance = ZERO
    if remaining > 0:
        credit = get_or_create_credit(school, student)
        credit = StudentFeeCredit.objects.select_for_update().get(pk=credit.pk)
        credit.balance = (credit.balance or ZERO) + remaining
        credit.save(update_fields=['balance', 'updated_at'])
        StudentFeeCreditMovement.objects.create(
            school=school,
            credit=credit,
            kind=StudentFeeCreditMovement.Kind.OVERPAYMENT,
            amount=remaining,
            payment_transaction=payment_tx,
            note='Overpayment held as credit toward future fees',
        )
        credit_added = remaining
        credit_balance = credit.balance
        remaining = ZERO

    return AllocationResult(
        amount_received=amount,
        applied_to_invoices=applied,
        credit_added=credit_added,
        credit_balance=credit_balance,
    )


@transaction.atomic
def apply_available_credit_to_invoice(invoice: FeeInvoice) -> Decimal:
    """Apply wallet credit to an unpaid invoice. Returns amount applied."""
    if invoice.balance <= 0:
        return ZERO
    credit = (
        StudentFeeCredit.objects.select_for_update()
        .filter(school_id=invoice.school_id, student_id=invoice.student_id)
        .first()
    )
    if credit is None or credit.balance <= 0:
        return ZERO
    take = min(credit.balance, invoice.balance)
    if take <= 0:
        return ZERO
    invoice.apply_successful_payment(take, save=True)
    credit.balance -= take
    credit.save(update_fields=['balance', 'updated_at'])
    StudentFeeCreditMovement.objects.create(
        school_id=invoice.school_id,
        credit=credit,
        kind=StudentFeeCreditMovement.Kind.APPLIED,
        amount=-take,
        invoice=invoice,
        note=f'Applied to {invoice.term}',
    )
    return take


@transaction.atomic
def allow_credit_refund(*, credit: StudentFeeCredit, actor=None) -> StudentFeeCredit:
    credit.refund_allowed = True
    credit.refund_allowed_at = timezone.now()
    credit.refund_allowed_by = actor if getattr(actor, 'is_authenticated', False) else None
    credit.refund_collected_at = None
    credit.save(
        update_fields=[
            'refund_allowed',
            'refund_allowed_at',
            'refund_allowed_by',
            'refund_collected_at',
            'updated_at',
        ]
    )
    StudentFeeCreditMovement.objects.create(
        school_id=credit.school_id,
        credit=credit,
        kind=StudentFeeCreditMovement.Kind.REFUND_ALLOWED,
        amount=ZERO,
        note='Staff authorized school-visit refund',
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
    )
    return credit


@transaction.atomic
def mark_credit_refund_collected(*, credit: StudentFeeCredit, actor=None) -> StudentFeeCredit:
    amount = credit.balance or ZERO
    credit.balance = ZERO
    credit.refund_allowed = False
    credit.refund_collected_at = timezone.now()
    credit.save(
        update_fields=[
            'balance',
            'refund_allowed',
            'refund_collected_at',
            'updated_at',
        ]
    )
    StudentFeeCreditMovement.objects.create(
        school_id=credit.school_id,
        credit=credit,
        kind=StudentFeeCreditMovement.Kind.REFUND_COLLECTED,
        amount=-amount,
        note='Refund collected at school office',
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
    )
    return credit


def notify_parent_payment_allocation(*, payment_tx, allocation: AllocationResult) -> None:
    """Append credit notice onto the normal receipt WhatsApp when overpaid."""
    if not allocation.has_credit:
        return
    from communications.services.twilio_service import send_whatsapp_message
    from finance.services.receipts import format_kes

    student = payment_tx.invoice.student
    to_phone = (payment_tx.phone_number or student.parent_phone or '').strip()
    if not to_phone:
        return
    body = (
        f'*{payment_tx.school.name} — payment note*\n\n'
        f'Asante. We received {format_kes(allocation.amount_received)} '
        f'for {student.full_name}.\n\n'
        f'{format_kes(allocation.credit_added)} exceeds the current fees due, '
        f'so it is held as credit for the next term '
        f'(credit balance: {format_kes(allocation.credit_balance)}).\n\n'
        f'If you need a refund instead, please contact the school office.'
    )
    try:
        send_whatsapp_message(
            payment_tx.school,
            to_phone,
            body,
            sender_type='BOT',
        )
    except Exception:
        logger.exception(
            'Credit notice WhatsApp failed tx=%s',
            payment_tx.pk,
        )


def notify_parent_refund_visit(*, credit: StudentFeeCredit) -> tuple[bool, str]:
    from communications.services.twilio_service import send_whatsapp_message
    from finance.services.receipts import format_kes

    student = credit.student
    to_phone = (student.parent_phone or '').strip()
    if not to_phone:
        return False, 'No parent phone on student.'
    body = (
        f'*{credit.school.name} — refund available*\n\n'
        f'A fee credit of {format_kes(credit.balance)} is available for '
        f'{student.full_name} (Adm {student.admission_number}).\n\n'
        f'Please visit the school office to collect your refund. '
        f'Bring a valid ID.'
    )
    return send_whatsapp_message(
        credit.school,
        to_phone,
        body,
        sender_type='BOT',
    )
