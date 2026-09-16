"""Deterministic Daraja STK callback reconciliation."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction

from finance.models import FeeInvoice, LedgerEntry, PaymentTransaction

logger = logging.getLogger(__name__)

ACK = {'ResultCode': 0, 'ResultDesc': 'Accepted'}

TERMINAL_STATUSES = frozenset(
    {
        PaymentTransaction.Status.SUCCESS,
        PaymentTransaction.Status.FAILED_USER,
        PaymentTransaction.Status.FAILED_TIMEOUT,
    }
)

# Safaricom result codes we map explicitly; anything else → FAILED_ERROR.
RESULT_USER_CANCELLED = 1032
RESULT_TIMEOUT = 1037
RESULT_INSUFFICIENT = 1


def _extract_stk_callback(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    body = payload.get('Body')
    if isinstance(body, dict) and isinstance(body.get('stkCallback'), dict):
        return body['stkCallback']
    if isinstance(payload.get('stkCallback'), dict):
        return payload['stkCallback']
    return {}


def _metadata_map(stk_callback: dict[str, Any]) -> dict[str, Any]:
    meta = stk_callback.get('CallbackMetadata') or {}
    items = meta.get('Item') or []
    out: dict[str, Any] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get('Name')
        if name:
            out[str(name)] = item.get('Value')
    return out


def _to_decimal(value) -> Decimal | None:
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _map_failure_status(result_code: int) -> str:
    if result_code == RESULT_USER_CANCELLED:
        return PaymentTransaction.Status.FAILED_USER
    if result_code == RESULT_TIMEOUT:
        return PaymentTransaction.Status.FAILED_TIMEOUT
    # Spec: ResultCode in [1032, 1037, 1] — map 1 and other codes to FAILED_ERROR
    return PaymentTransaction.Status.FAILED_ERROR


def _phone_digits(phone: str) -> str:
    return ''.join(ch for ch in (phone or '') if ch.isdigit())


def _phones_match(expected: str, actual: str) -> bool:
    a = _phone_digits(expected)
    b = _phone_digits(actual)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 9 and len(b) >= 9 and a[-9:] == b[-9:]


def _mark_stk_mismatch(
    payment_tx,
    *,
    payload,
    result_code,
    result_desc,
    reason: str,
):
    logger.warning(
        'STK callback mismatch checkout=%s school=%s reason=%s',
        payment_tx.checkout_request_id,
        payment_tx.school_id,
        reason,
    )
    payment_tx.raw_callback_payload = payload
    payment_tx.result_code = result_code
    payment_tx.result_desc = f'{result_desc} ({reason})'.strip()
    payment_tx.status = PaymentTransaction.Status.FAILED_ERROR
    payment_tx.save(
        update_fields=[
            'raw_callback_payload',
            'result_code',
            'result_desc',
            'status',
            'updated_at',
        ]
    )
    return ACK


@transaction.atomic
def reconcile_stk_callback(
    *,
    payload: dict[str, Any],
    tenant_id: str | None = None,
) -> dict[str, str | int]:
    """
    Apply an STK callback to PaymentTransaction + FeeInvoice idempotently.

    Requires tenant_id. On success, amount and phone must match the pending tx.
    Always safe to acknowledge to Safaricom after this returns.
    """
    if not tenant_id:
        logger.warning('Daraja callback missing tenant_id')
        return ACK

    stk = _extract_stk_callback(payload)
    checkout_id = (stk.get('CheckoutRequestID') or '').strip()
    if not checkout_id:
        logger.warning('Daraja callback missing CheckoutRequestID')
        return ACK

    try:
        result_code = int(stk.get('ResultCode'))
    except (TypeError, ValueError):
        logger.warning(
            'Daraja callback missing/invalid ResultCode checkout=%s',
            checkout_id,
        )
        return ACK

    result_desc = stk.get('ResultDesc') or ''

    payment_tx = (
        PaymentTransaction.objects.select_for_update()
        .filter(
            checkout_request_id=checkout_id,
            school_id=tenant_id,
        )
        .first()
    )
    if payment_tx is None:
        logger.info(
            'Daraja callback: no PaymentTransaction for checkout=%s tenant=%s',
            checkout_id,
            tenant_id,
        )
        return ACK

    if payment_tx.status in TERMINAL_STATUSES:
        return ACK

    payment_tx.raw_callback_payload = payload
    payment_tx.result_code = result_code
    payment_tx.result_desc = result_desc

    if result_code == 0:
        meta = _metadata_map(stk)
        receipt = (meta.get('MpesaReceiptNumber') or '').strip()
        amount = _to_decimal(meta.get('Amount'))
        callback_phone = str(meta.get('PhoneNumber') or '').strip()

        if amount is None:
            return _mark_stk_mismatch(
                payment_tx,
                payload=payload,
                result_code=result_code,
                result_desc=result_desc,
                reason='missing_amount',
            )
        if amount != payment_tx.amount:
            return _mark_stk_mismatch(
                payment_tx,
                payload=payload,
                result_code=result_code,
                result_desc=result_desc,
                reason='amount_mismatch',
            )
        if not _phones_match(payment_tx.phone_number, callback_phone):
            return _mark_stk_mismatch(
                payment_tx,
                payload=payload,
                result_code=result_code,
                result_desc=result_desc,
                reason='phone_mismatch',
            )

        invoice = (
            FeeInvoice.objects.select_for_update()
            .select_related('student')
            .get(pk=payment_tx.invoice_id)
        )

        payment_tx.mpesa_receipt_number = receipt or None
        payment_tx.amount = amount
        payment_tx.status = PaymentTransaction.Status.SUCCESS
        # Avoid model save side-effects re-applying invoice balance; do it here.
        payment_tx.save(
            update_fields=[
                'raw_callback_payload',
                'result_code',
                'result_desc',
                'mpesa_receipt_number',
                'amount',
                'status',
                'updated_at',
            ]
        )

        LedgerEntry.create_from_transaction(payment_tx)

        from finance.services.credits import (
            apply_payment_across_invoices,
            notify_parent_payment_allocation,
        )
        from finance.services.receipts import queue_receipt_whatsapp
        from tenants.audit import log_audit_event
        from tenants.models import AuditEvent

        allocation = apply_payment_across_invoices(
            school=payment_tx.school,
            student=invoice.student,
            amount=amount,
            payment_tx=payment_tx,
            primary_invoice=invoice,
        )
        invoice.refresh_from_db()

        log_audit_event(
            payment_tx.school,
            category=AuditEvent.Category.PAYMENT,
            action='stk_success',
            summary=(
                f'M-Pesa STK {payment_tx.mpesa_receipt_number or payment_tx.pk}: '
                f'{amount} for {invoice.student.admission_number}'
            ),
            object_type='PaymentTransaction',
            object_id=str(payment_tx.pk),
            metadata={
                'amount': str(amount),
                'receipt': payment_tx.mpesa_receipt_number or '',
                'invoice_id': str(invoice.pk),
                'credit_added': str(allocation.credit_added),
                'allocations': [
                    {**row, 'amount': str(row['amount'])}
                    for row in allocation.applied_to_invoices
                ],
            },
        )

        def _after_commit(pk=payment_tx.pk, alloc=allocation):
            queue_receipt_whatsapp(pk)
            from finance.models import PaymentTransaction as PT

            tx = PT.objects.filter(pk=pk).select_related(
                'invoice', 'invoice__student', 'school'
            ).first()
            if tx is not None:
                notify_parent_payment_allocation(payment_tx=tx, allocation=alloc)

        transaction.on_commit(_after_commit)
        return ACK

    if result_code in (RESULT_USER_CANCELLED, RESULT_TIMEOUT, RESULT_INSUFFICIENT):
        payment_tx.status = _map_failure_status(result_code)
    else:
        payment_tx.status = PaymentTransaction.Status.FAILED_ERROR

    payment_tx.save(
        update_fields=[
            'raw_callback_payload',
            'result_code',
            'result_desc',
            'status',
            'updated_at',
        ]
    )
    return ACK


C2B_ACCEPT = {'ResultCode': 0, 'ResultDesc': 'Accepted'}
C2B_REJECT = {'ResultCode': 'C2B00011', 'ResultDesc': 'Rejected'}


def _normalize_bill_ref(bill_ref: str) -> str:
    ref = (bill_ref or '').strip().upper()
    if ref.startswith('ADM-'):
        ref = ref[4:].strip()
    return ref


def _e164_from_msisdn(msisdn: str) -> str:
    raw = (msisdn or '').strip().replace(' ', '')
    if raw.startswith('+'):
        return raw
    if raw.startswith('0') and len(raw) == 10:
        return f'+254{raw[1:]}'
    if raw.startswith('254') and len(raw) == 12:
        return f'+{raw}'
    if raw:
        return f'+{raw}' if not raw.startswith('+') else raw
    return '+254700000000'


def resolve_student_for_bill_ref(school, bill_ref: str):
    from academics.models import Student

    ref = _normalize_bill_ref(bill_ref)
    if not ref:
        return None
    return (
        Student.objects.filter(
            school=school,
            admission_number__iexact=ref,
            is_active=True,
        ).first()
        or Student.objects.filter(
            school=school,
            admission_number__iexact=bill_ref.strip(),
            is_active=True,
        ).first()
    )


def validate_c2b_payment(*, payload: dict[str, Any], tenant_id: str | None):
    """
    Safaricom C2B validation: accept only when BillRefNumber matches a student.
    """
    from tenants.models import School

    if not tenant_id:
        return C2B_REJECT
    school = School.objects.filter(pk=tenant_id, is_active=True).first()
    if school is None:
        return C2B_REJECT

    bill_ref = str(payload.get('BillRefNumber') or '')
    student = resolve_student_for_bill_ref(school, bill_ref)
    if student is None:
        logger.info(
            'C2B validation rejected school=%s bill_ref=%s',
            tenant_id,
            bill_ref,
        )
        return C2B_REJECT
    return C2B_ACCEPT


@transaction.atomic
def reconcile_c2b_confirmation(
    *,
    payload: dict[str, Any],
    tenant_id: str | None = None,
) -> dict[str, str | int]:
    """
    Apply a Paybill C2B confirmation to the matching student's open invoice.
    Idempotent on MpesaReceiptNumber / TransID.
    """
    from tenants.models import School

    if not tenant_id:
        logger.warning('C2B confirmation missing tenant_id')
        return C2B_ACCEPT

    school = School.objects.filter(pk=tenant_id, is_active=True).first()
    if school is None:
        logger.warning('C2B confirmation unknown school=%s', tenant_id)
        return C2B_ACCEPT

    trans_id = str(payload.get('TransID') or '').strip()
    if not trans_id:
        logger.warning('C2B confirmation missing TransID')
        return C2B_ACCEPT

    existing = PaymentTransaction.objects.filter(
        school=school,
        mpesa_receipt_number=trans_id,
    ).first()
    if existing is not None:
        return C2B_ACCEPT

    bill_ref = str(payload.get('BillRefNumber') or '')
    student = resolve_student_for_bill_ref(school, bill_ref)
    if student is None:
        logger.warning(
            'C2B confirmation unmatched bill_ref=%s school=%s',
            bill_ref,
            tenant_id,
        )
        return C2B_ACCEPT

    amount = _to_decimal(payload.get('TransAmount'))
    if amount is None or amount <= 0:
        logger.warning('C2B confirmation invalid amount trans=%s', trans_id)
        return C2B_ACCEPT

    invoice = (
        FeeInvoice.objects.select_for_update()
        .filter(school=school, student=student)
        .exclude(status=FeeInvoice.Status.PAID)
        .order_by('due_date', 'created_at')
        .first()
        or FeeInvoice.objects.select_for_update()
        .filter(school=school, student=student)
        .order_by('-due_date', '-created_at')
        .first()
    )
    if invoice is None:
        logger.warning(
            'C2B confirmation no invoice student=%s school=%s',
            student.admission_number,
            tenant_id,
        )
        return C2B_ACCEPT

    phone = _e164_from_msisdn(str(payload.get('MSISDN') or ''))
    if len(phone) < 10:
        phone = student.parent_phone or '+254700000000'
    payment_tx = PaymentTransaction(
        school=school,
        invoice=invoice,
        phone_number=phone,
        amount=amount,
        mpesa_receipt_number=trans_id,
        status=PaymentTransaction.Status.SUCCESS,
        result_code=0,
        result_desc='C2B Paybill confirmation',
        raw_callback_payload=payload,
    )
    payment_tx.save()

    from finance.services.credits import (
        apply_payment_across_invoices,
        notify_parent_payment_allocation,
    )
    from finance.services.receipts import queue_receipt_whatsapp
    from tenants.audit import log_audit_event
    from tenants.models import AuditEvent

    allocation = apply_payment_across_invoices(
        school=school,
        student=student,
        amount=amount,
        payment_tx=payment_tx,
        primary_invoice=invoice,
    )

    log_audit_event(
        school,
        category=AuditEvent.Category.PAYMENT,
        action='c2b_success',
        summary=(
            f'M-Pesa C2B {trans_id}: {amount} for {student.admission_number}'
        ),
        object_type='PaymentTransaction',
        object_id=str(payment_tx.pk),
        metadata={
            'amount': str(amount),
            'receipt': trans_id,
            'invoice_id': str(invoice.pk),
            'credit_added': str(allocation.credit_added),
            'allocations': [
                {**row, 'amount': str(row['amount'])}
                for row in allocation.applied_to_invoices
            ],
        },
    )

    def _after_commit(pk=payment_tx.pk, alloc=allocation):
        queue_receipt_whatsapp(pk)
        from finance.models import PaymentTransaction as PT

        tx = PT.objects.filter(pk=pk).select_related(
            'invoice', 'invoice__student', 'school'
        ).first()
        if tx is not None:
            notify_parent_payment_allocation(payment_tx=tx, allocation=alloc)

    transaction.on_commit(_after_commit)
    return C2B_ACCEPT
