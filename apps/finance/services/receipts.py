"""Digital fee receipts: WhatsApp text + printable/PDF context."""

from __future__ import annotations

import logging
from decimal import Decimal
from io import BytesIO

from django.utils import timezone

logger = logging.getLogger(__name__)


def format_kes(amount) -> str:
    try:
        value = Decimal(str(amount or '0'))
    except Exception:
        value = Decimal('0')
    return f'KES {value:,.0f}' if value == value.to_integral_value() else f'KES {value:,.2f}'


def _student_outstanding_total(school, student) -> Decimal:
    from finance.models import FeeInvoice

    total = Decimal('0')
    for inv in FeeInvoice.objects.filter(school=school, student=student).exclude(
        status=FeeInvoice.Status.PAID
    ):
        bal = inv.balance
        if bal > 0:
            total += bal
    return total


def build_receipt_whatsapp_body(payment_tx) -> str:
    from communications.services.whatsapp_delivery import local_now

    invoice = payment_tx.invoice
    student = invoice.student
    school = payment_tx.school
    receipt_no = payment_tx.mpesa_receipt_number or f'TX-{payment_tx.pk}'
    paid_at = local_now(now=payment_tx.updated_at or payment_tx.created_at)
    outstanding = _student_outstanding_total(school, student)

    lines = [
        f'*{school.name} — payment received*',
        '',
        f'Asante. We received *{format_kes(payment_tx.amount)}* for '
        f'*{student.full_name}* (Adm {student.admission_number}).',
        f'Receipt: {receipt_no}',
        f'Date: {paid_at.strftime("%d %b %Y %H:%M")}',
    ]

    if outstanding > 0:
        lines.extend(
            [
                '',
                f'Remaining balance: *{format_kes(outstanding)}*',
                '',
                'When can you clear the rest?',
                'Reply with a date (e.g. *tomorrow*, *Friday*), or say '
                '*not sure* if you do not know yet.',
                'Reply *menu* for other payment options.',
            ]
        )
    else:
        lines.extend(
            [
                '',
                'Remaining balance: *KES 0* — fees are cleared. Asante!',
            ]
        )
    return '\n'.join(lines)


def send_payment_receipt_whatsapp(
    payment_tx,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Send a WhatsApp receipt to the payer / parent phone.
    Idempotent unless force=True (staff resend).
    """
    from communications.services.identity import (
        get_or_create_active_session,
        resolve_parent_identity,
    )
    from communications.services.twilio_service import (
        TwilioConfigError,
        parent_whatsapp_destination,
        send_whatsapp_message,
    )

    try:
        payment_tx.refresh_from_db()
    except Exception:
        pass

    if payment_tx.status != payment_tx.Status.SUCCESS:
        return False, 'Payment is not successful.'

    if payment_tx.receipt_whatsapp_sent_at and not force:
        return True, 'Receipt already sent.'

    invoice = payment_tx.invoice
    student = invoice.student
    roster_phone = (student.parent_phone or payment_tx.phone_number or '').strip()
    if not roster_phone:
        return False, 'No phone number for receipt.'

    session = None
    to_dest = roster_phone
    try:
        parent = resolve_parent_identity(payment_tx.school, roster_phone)
        if parent.reminders_paused_at is not None:
            return False, 'Parent has paused WhatsApp messages (STOP).'
        session = get_or_create_active_session(payment_tx.school, parent)
        if session.active_student_id != student.pk:
            session.active_student = student
            session.save(update_fields=['active_student'])
        to_dest = parent_whatsapp_destination(parent)
    except ValueError:
        session = None
        to_dest = roster_phone

    body = build_receipt_whatsapp_body(payment_tx)
    try:
        ok, detail = send_whatsapp_message(
            payment_tx.school,
            to_dest,
            body,
            session=session,
            sender_type='BOT',
        )
    except (TwilioConfigError, ValueError) as exc:
        logger.info('Receipt WhatsApp skipped tx=%s: %s', payment_tx.pk, exc)
        return False, str(exc)
    except Exception as exc:
        logger.exception('Receipt WhatsApp failed tx=%s', payment_tx.pk)
        return False, str(exc)

    if ok:
        type(payment_tx).objects.filter(pk=payment_tx.pk).update(
            receipt_whatsapp_sent_at=timezone.now()
        )
        outstanding = _student_outstanding_total(payment_tx.school, student)
        if session is not None and outstanding > 0:
            session.agent_awaiting = 'promise_date'
            session.save(update_fields=['agent_awaiting'])
        elif session is not None:
            session.agent_awaiting = ''
            session.save(update_fields=['agent_awaiting'])
    return ok, detail


def queue_receipt_whatsapp(payment_tx_id: int) -> None:
    """Load payment and send receipt; for use with transaction.on_commit."""
    from finance.models import PaymentTransaction

    payment_tx = (
        PaymentTransaction.objects.select_related(
            'school',
            'invoice',
            'invoice__student',
        )
        .filter(pk=payment_tx_id)
        .first()
    )
    if payment_tx is None:
        return
    ok, detail = send_payment_receipt_whatsapp(payment_tx, force=False)
    if ok:
        logger.info('Receipt WhatsApp sent tx=%s', payment_tx_id)
    else:
        logger.info('Receipt WhatsApp not sent tx=%s: %s', payment_tx_id, detail)


def render_receipt_pdf(payment_tx) -> bytes:
    """Minimal single-page PDF receipt (reportlab)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from communications.services.whatsapp_delivery import local_now

    invoice = payment_tx.invoice
    student = invoice.student
    school = payment_tx.school
    receipt_no = payment_tx.mpesa_receipt_number or f'TX-{payment_tx.pk}'
    paid_at = local_now(now=payment_tx.updated_at or payment_tx.created_at)
    outstanding = _student_outstanding_total(school, student)

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 72
    c.setFont('Helvetica-Bold', 16)
    c.drawString(72, y, f'{school.name}')
    y -= 28
    c.setFont('Helvetica', 11)
    for line in (
        'Fee payment receipt',
        f'Receipt: {receipt_no}',
        f'Date: {paid_at.strftime("%d %b %Y %H:%M")}',
        f'Student: {student.full_name}',
        f'Admission: {student.admission_number}',
        f'Term: {invoice.term}',
        f'Amount paid: {format_kes(payment_tx.amount)}',
        f'Remaining balance: {format_kes(outstanding)}',
    ):
        c.drawString(72, y, line)
        y -= 18
    c.showPage()
    c.save()
    return buffer.getvalue()


def notify_parent_discount_applied(
    *,
    invoice,
    applied_amount,
    note: str = '',
    waive: bool = False,
) -> tuple[bool, str]:
    """
    WhatsApp the parent that a bursary / discount / waiver was applied.
    """
    from communications.services.identity import (
        get_or_create_active_session,
        resolve_parent_identity,
    )
    from communications.services.twilio_service import (
        TwilioConfigError,
        parent_whatsapp_destination,
        send_whatsapp_message,
    )

    student = invoice.student
    school = invoice.school
    roster_phone = (student.parent_phone or '').strip()
    if not roster_phone:
        return False, 'No parent phone on student.'

    try:
        applied = Decimal(str(applied_amount or '0'))
    except Exception:
        applied = Decimal('0')
    if applied <= 0:
        return False, 'Nothing to notify.'

    session = None
    try:
        parent = resolve_parent_identity(school, roster_phone)
        if parent.reminders_paused_at is not None:
            return False, 'Parent has paused WhatsApp messages (STOP).'
        session = get_or_create_active_session(school, parent)
        if session.active_student_id != student.pk:
            session.active_student = student
            session.save(update_fields=['active_student'])
        to_dest = parent_whatsapp_destination(parent)
    except ValueError as exc:
        return False, str(exc)

    invoice.refresh_from_db()
    outstanding = _student_outstanding_total(school, student)
    kind = 'fee waiver' if waive else 'bursary / discount'
    note_line = f'\nReason: {note.strip()}' if (note or '').strip() else ''

    lines = [
        f'*{school.name} — good news*',
        '',
        f'A {kind} of *{format_kes(applied)}* has been applied for '
        f'*{student.full_name}* (Adm {student.admission_number}) '
        f'on *{invoice.term}*.{note_line}',
        '',
    ]
    if outstanding > 0:
        lines.extend(
            [
                f'Remaining balance: *{format_kes(outstanding)}*',
                '',
                'Reply *menu* if you would like payment options, '
                'or *1* to pay by M-Pesa.',
            ]
        )
    else:
        lines.append(
            'Remaining balance: *KES 0* — this term is cleared. Asante!'
        )

    body = '\n'.join(lines)
    try:
        ok, detail = send_whatsapp_message(
            school,
            to_dest,
            body,
            session=session,
            sender_type='BOT',
        )
    except (TwilioConfigError, ValueError) as exc:
        logger.info(
            'Discount WhatsApp skipped invoice=%s: %s',
            invoice.pk,
            exc,
        )
        return False, str(exc)
    except Exception as exc:
        logger.exception('Discount WhatsApp failed invoice=%s', invoice.pk)
        return False, str(exc)

    if ok and session is not None and outstanding > 0:
        session.agent_awaiting = 'menu'
        session.save(update_fields=['agent_awaiting'])
    return ok, detail
