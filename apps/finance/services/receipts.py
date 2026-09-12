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
    return f'KES {value:,.2f}'


def build_receipt_whatsapp_body(payment_tx) -> str:
    from communications.services.whatsapp_delivery import local_now

    invoice = payment_tx.invoice
    student = invoice.student
    school = payment_tx.school
    receipt_no = payment_tx.mpesa_receipt_number or f'TX-{payment_tx.pk}'
    paid_at = local_now(now=payment_tx.updated_at or payment_tx.created_at)
    discount = invoice.discount_amount or Decimal('0')
    lines = [
        f'*{school.name} — Fee receipt*',
        '',
        f'Receipt: {receipt_no}',
        f'Date: {paid_at.strftime("%d %b %Y %H:%M")}',
        f'Student: {student.full_name}',
        f'Admission: {student.admission_number}',
        f'Term: {invoice.term}',
        f'Amount paid: {format_kes(payment_tx.amount)}',
    ]
    if discount > 0:
        lines.append(f'Bursary/waiver on invoice: {format_kes(discount)}')
    lines.extend(
        [
            f'Invoice balance: {format_kes(invoice.balance)}',
            '',
            'Asante. Keep this message as your receipt.',
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
    to_phone = (payment_tx.phone_number or student.parent_phone or '').strip()
    if not to_phone:
        return False, 'No phone number for receipt.'

    session = None
    try:
        parent = resolve_parent_identity(payment_tx.school, to_phone)
        if parent.reminders_paused_at is not None:
            return False, 'Parent has paused WhatsApp messages (STOP).'
        session = get_or_create_active_session(payment_tx.school, parent)
        if session.active_student_id != student.pk:
            session.active_student = student
            session.save(update_fields=['active_student'])
    except ValueError:
        session = None

    body = build_receipt_whatsapp_body(payment_tx)
    try:
        ok, detail = send_whatsapp_message(
            payment_tx.school,
            to_phone,
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
        f'Invoice balance: {format_kes(invoice.balance)}',
    ):
        c.drawString(72, y, line)
        y -= 18
    c.showPage()
    c.save()
    return buffer.getvalue()
