"""Autonomous outbound fee reminder dispatch."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F
from django.utils import timezone

from communications.models import ConversationSession, ParentContact
from communications.services.identity import (
    get_or_create_active_session,
    resolve_parent_identity,
)
from communications.services.twilio_service import send_whatsapp_message
from finance.models import FeeInvoice, PaymentPromise

logger = logging.getLogger(__name__)

BALANCE_THRESHOLD = Decimal('50.00')
CONTACT_COOLDOWN = timedelta(hours=72)


@dataclass
class ReminderDispatchResult:
    scanned: int = 0
    reminded: int = 0
    skipped_cooldown: int = 0
    skipped_escalated_or_promise: int = 0
    skipped_other: int = 0

    def as_console_line(self, school_name: str) -> str:
        return (
            f'[Tenant: {school_name}] '
            f'Scanned: {self.scanned}, '
            f'Reminded: {self.reminded}, '
            f'Skipped (Cooldown): {self.skipped_cooldown}, '
            f'Skipped (Escalated/Promise): {self.skipped_escalated_or_promise}'
        )


class FeeReminderService:
    """Select overdue invoices and send WhatsApp fee reminders with guardrails."""

    balance_threshold = BALANCE_THRESHOLD
    contact_cooldown = CONTACT_COOLDOWN

    def dispatch_overdue_reminders(self, school) -> ReminderDispatchResult:
        today = timezone.localdate()
        now = timezone.now()
        cooldown_cutoff = now - self.contact_cooldown
        result = ReminderDispatchResult()

        balance_expr = ExpressionWrapper(
            F('total_amount') - F('paid_amount'),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        invoices = list(
            FeeInvoice.objects.filter(
                school=school,
                due_date__lte=today,
                student__is_active=True,
            )
            .exclude(status=FeeInvoice.Status.PAID)
            .annotate(computed_balance=balance_expr)
            .filter(computed_balance__gt=self.balance_threshold)
            .select_related('student')
            .order_by('due_date', 'student__admission_number')
        )
        result.scanned = len(invoices)

        # Parent-level cooldown: once reminded this run, skip siblings.
        contacted_parent_ids: set[int] = set()

        for invoice in invoices:
            student = invoice.student
            phone = (student.parent_phone or '').strip()
            if not phone:
                result.skipped_other += 1
                continue

            has_promise = PaymentPromise.objects.filter(
                school=school,
                invoice__student=student,
                status=PaymentPromise.Status.PENDING,
                promised_date__gte=today,
            ).exists()
            if has_promise:
                result.skipped_escalated_or_promise += 1
                continue

            try:
                parent = resolve_parent_identity(school, phone)
            except ValueError:
                logger.warning(
                    'Reminder skip: invalid parent phone school=%s student=%s',
                    school.pk,
                    student.admission_number,
                )
                result.skipped_other += 1
                continue

            if parent.pk in contacted_parent_ids:
                result.skipped_cooldown += 1
                continue

            if (
                parent.last_contacted_at is not None
                and parent.last_contacted_at >= cooldown_cutoff
            ):
                result.skipped_cooldown += 1
                continue

            blocking_session = ConversationSession.objects.filter(
                school=school,
                parent_contact=parent,
                status__in=[
                    ConversationSession.Status.ESCALATED_PENDING,
                    ConversationSession.Status.STAFF_ACTIVE,
                ],
            ).exists()
            if blocking_session:
                result.skipped_escalated_or_promise += 1
                continue

            session = get_or_create_active_session(school, parent)
            if session.status in (
                ConversationSession.Status.ESCALATED_PENDING,
                ConversationSession.Status.STAFF_ACTIVE,
            ):
                result.skipped_escalated_or_promise += 1
                continue

            if session.active_student_id != student.pk:
                session.active_student = student
                session.save(update_fields=['active_student'])

            body = self._format_reminder_message(
                school=school,
                student=student,
                invoice=invoice,
                balance=invoice.computed_balance,
            )
            ok, detail = send_whatsapp_message(
                school,
                parent.phone_number,
                body,
                session=session,
                sender_type='BOT',
            )
            if not ok:
                logger.warning(
                    'Fee reminder send failed school=%s student=%s: %s',
                    school.pk,
                    student.admission_number,
                    detail,
                )
                result.skipped_other += 1
                continue

            ParentContact.objects.filter(pk=parent.pk).update(last_contacted_at=now)
            FeeInvoice.objects.filter(pk=invoice.pk).update(last_contacted_at=now)
            parent.last_contacted_at = now
            invoice.last_contacted_at = now
            contacted_parent_ids.add(parent.pk)
            result.reminded += 1

        return result

    def _format_reminder_message(self, *, school, student, invoice, balance) -> str:
        parent_label = (student.parent_name or '').strip() or 'Parent'
        paybill = (getattr(school, 'paybill_number', None) or '').strip() or 'N/A'
        school_name = school.name
        return (
            f'Habari {parent_label},\n\n'
            f'This is a fee reminder from {school_name}.\n\n'
            f'Student: {student.full_name}\n'
            f'Admission No: {student.admission_number}\n'
            f'Term: {invoice.term}\n'
            f'Outstanding balance: KSh {balance:,.2f}\n'
            f'Paybill: {paybill}\n'
            f'Account: {student.admission_number}\n\n'
            f'Please settle the outstanding balance at your earliest convenience. '
            f'Reply to this chat if you need help or want to arrange a payment promise.'
        )
