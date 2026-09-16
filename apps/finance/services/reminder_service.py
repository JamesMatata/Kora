"""Autonomous outbound fee reminder dispatch (Kitabu-style daily menu reminders)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Q
from django.utils import timezone

from communications.models import ConversationSession, MessageLog, ParentContact

from communications.services.identity import (
    get_or_create_active_session,
    resolve_parent_identity,
)
from communications.services.twilio_service import send_whatsapp_message
from communications.services.whatsapp_delivery import (
    contacted_on_local_date,
    format_date_human,
    format_kes,
    in_quiet_period,
    local_now,
    local_today,
    local_week_start,
    next_delivery_at,
    sanitize_outbound_text,
)
from finance.models import FeeInvoice, PaymentPromise

logger = logging.getLogger(__name__)

BALANCE_THRESHOLD = Decimal('50.00')
# Match Kitabu: at most one reminder WhatsApp per parent per local day.
BATCH_LIMIT = 80
# Hard weekly cap (Nairobi Mon–Sun), on top of the daily cap.
MAX_REMINDERS_PER_WEEK = 2
# Stable signature used to count fee-reminder MessageLogs (not agent chatter).
REMINDER_BODY_MARKER = 'Reply *STOP* to pause reminders.'
# Soft pause when parent said "not sure" about when they will pay.
UNCERTAIN_PROMISE_GRACE_DAYS = 14


def _student_has_blocking_promise(*, school, student, today) -> bool:
    grace_start = timezone.now() - timedelta(days=UNCERTAIN_PROMISE_GRACE_DAYS)
    return (
        PaymentPromise.objects.filter(
            school=school,
            invoice__student=student,
            status=PaymentPromise.Status.PENDING,
        )
        .filter(
            Q(promised_date__gte=today)
            | Q(date_uncertain=True, created_at__gte=grace_start)
        )
        .exists()
    )


@dataclass
class ReminderDispatchResult:
    scanned: int = 0
    reminded: int = 0
    skipped_cooldown: int = 0
    skipped_escalated_or_promise: int = 0
    skipped_quiet_hours: int = 0
    skipped_paused: int = 0
    skipped_other: int = 0

    def as_console_line(self, school_name: str) -> str:
        return (
            f'[Tenant: {school_name}] '
            f'Scanned: {self.scanned}, '
            f'Reminded: {self.reminded}, '
            f'Skipped (Cooldown): {self.skipped_cooldown}, '
            f'Skipped (Paused): {self.skipped_paused}, '
            f'Skipped (Quiet hours): {self.skipped_quiet_hours}, '
            f'Skipped (Escalated/Promise): {self.skipped_escalated_or_promise}'
        )


class FeeReminderService:
    """Select overdue invoices and send WhatsApp fee reminders with Kitabu guardrails."""

    balance_threshold = BALANCE_THRESHOLD
    batch_limit = BATCH_LIMIT
    max_reminders_per_week = MAX_REMINDERS_PER_WEEK

    def _reminders_sent_this_week(self, school, parent, *, week_start) -> int:
        return MessageLog.objects.filter(
            school=school,
            session__parent_contact=parent,
            direction=MessageLog.Direction.OUTBOUND,
            sender=MessageLog.Sender.BOT,
            created_at__gte=week_start,
            body__contains=REMINDER_BODY_MARKER,
        ).exclude(
            delivery_status=MessageLog.DeliveryStatus.FAILED,
        ).count()

    def dispatch_overdue_reminders(
        self,
        school,
        *,
        force: bool = False,
    ) -> ReminderDispatchResult:
        today = local_today()
        now = local_now()
        week_start = local_week_start(now=now)
        result = ReminderDispatchResult()

        if not force and in_quiet_period(now=now, school=school):
            # Cron / Settings should run outside quiet hours.
            logger.info(
                'Fee reminders deferred school=%s quiet_hours until=%s',
                school.pk,
                next_delivery_at(now=now, school=school).isoformat(),
            )
            result.skipped_quiet_hours = 1
            return result

        balance_expr = ExpressionWrapper(
            F('total_amount') - F('discount_amount') - F('paid_amount'),
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

        # Group overdue invoices by parent → one menu WhatsApp per parent.
        by_parent: dict[int, dict] = {}

        for invoice in invoices:
            student = invoice.student
            phone = (student.parent_phone or '').strip()
            if not phone:
                result.skipped_other += 1
                continue

            if invoice.computed_balance <= 0:
                result.skipped_other += 1
                continue

            has_promise = _student_has_blocking_promise(
                school=school, student=student, today=today
            )
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

            bucket = by_parent.setdefault(
                parent.pk,
                {'parent': parent, 'invoices': []},
            )
            bucket['invoices'].append(invoice)

        contacted_this_run: set[int] = set()
        sent_count = 0

        for parent_id, bucket in by_parent.items():
            if sent_count >= self.batch_limit:
                break

            parent = bucket['parent']
            group = bucket['invoices']
            if not group:
                continue

            if parent_id in contacted_this_run:
                result.skipped_cooldown += 1
                continue

            # Opt-out is always honored — even when force=True (compliance).
            if parent.reminders_paused_at is not None:
                result.skipped_paused += 1
                continue

            if not force and contacted_on_local_date(
                parent.last_contacted_at,
                day=today,
            ):
                result.skipped_cooldown += 1
                continue

            # Weekly cap is always hard (including force / Run now).
            weekly_count = self._reminders_sent_this_week(
                school,
                parent,
                week_start=week_start,
            )
            if weekly_count >= self.max_reminders_per_week:
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

            from communications.services.identity import (
                clear_session_admission,
                confirm_session_admission,
            )

            student_ids = []
            for inv in group:
                if inv.student_id not in student_ids:
                    student_ids.append(inv.student_id)

            if len(student_ids) == 1:
                confirm_session_admission(session, group[0].student)
            else:
                # Parent must pick a child before pay options / agent balance.
                clear_session_admission(session)

            body = sanitize_outbound_text(
                self._format_reminder_message(
                    school=school,
                    invoices=group,
                )
            )
            if not body:
                result.skipped_other += 1
                continue

            from communications.services.twilio_service import parent_whatsapp_destination

            ok, detail = send_whatsapp_message(
                school,
                parent_whatsapp_destination(parent),
                body,
                session=session,
                sender_type='BOT',
            )
            if not ok:
                logger.warning(
                    'Fee reminder send failed school=%s parent=%s: %s',
                    school.pk,
                    parent.pk,
                    detail,
                )
                result.skipped_other += 1
                continue

            stamp = timezone.now()
            ParentContact.objects.filter(pk=parent.pk).update(last_contacted_at=stamp)
            # Fresh reminder menu — reset in-flight prompts so digits map to 1–5.
            ConversationSession.objects.filter(pk=session.pk).update(agent_awaiting='menu')
            FeeInvoice.objects.filter(
                pk__in=[inv.pk for inv in group],
            ).update(last_contacted_at=stamp)
            parent.last_contacted_at = stamp
            contacted_this_run.add(parent.pk)
            result.reminded += 1
            sent_count += 1

        return result

    def preview_overdue_reminders(self, school) -> dict:
        """
        Dry-run counts for Settings UI (no sends).
        """
        today = local_today()
        now = local_now()
        week_start = local_week_start(now=now)
        quiet = in_quiet_period(now=now, school=school)

        balance_expr = ExpressionWrapper(
            F('total_amount') - F('discount_amount') - F('paid_amount'),
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

        by_parent: dict[int, object] = {}
        for invoice in invoices:
            phone = (invoice.student.parent_phone or '').strip()
            if not phone:
                continue
            has_promise = _student_has_blocking_promise(
                school=school, student=invoice.student, today=today
            )
            if has_promise:
                continue
            try:
                parent = resolve_parent_identity(school, phone)
            except ValueError:
                continue
            by_parent.setdefault(parent.pk, parent)

        eligible = 0
        at_weekly_limit = 0
        would_be_final = 0
        already_today = 0
        paused = 0

        for parent in by_parent.values():
            if parent.reminders_paused_at is not None:
                paused += 1
                continue
            if contacted_on_local_date(parent.last_contacted_at, day=today):
                already_today += 1
                continue
            weekly = self._reminders_sent_this_week(
                school, parent, week_start=week_start
            )
            if weekly >= self.max_reminders_per_week:
                at_weekly_limit += 1
                continue
            if ConversationSession.objects.filter(
                school=school,
                parent_contact=parent,
                status__in=[
                    ConversationSession.Status.ESCALATED_PENDING,
                    ConversationSession.Status.STAFF_ACTIVE,
                ],
            ).exists():
                continue
            eligible += 1
            if weekly == self.max_reminders_per_week - 1:
                would_be_final += 1

        return {
            'in_quiet_hours': quiet,
            'next_window': next_delivery_at(now=now, school=school) if quiet else None,
            'eligible': eligible,
            'would_be_final_weekly': would_be_final,
            'at_weekly_limit': at_weekly_limit,
            'already_today': already_today,
            'paused': paused,
            'max_per_week': self.max_reminders_per_week,
        }

    def _format_reminder_message(self, *, school, invoices) -> str:
        """
        Natural fee reminder:
        - One student, one or more terms → term breakdown + pay menu
        - Several students → per-child totals/terms, then ask which child to pay for
        """
        from collections import OrderedDict

        by_student: OrderedDict[int, dict] = OrderedDict()
        for inv in invoices:
            bucket = by_student.setdefault(
                inv.student_id,
                {'student': inv.student, 'invoices': []},
            )
            bucket['invoices'].append(inv)

        entries = list(by_student.values())
        entries.sort(key=lambda e: (e['student'].admission_number or '').casefold())
        first_student = entries[0]['student']
        parent_label = (first_student.parent_name or '').strip() or 'Parent'
        first = parent_label.split()[0]
        school_name = school.name
        paybill = (getattr(school, 'paybill_number', None) or '').strip()

        def _term_lines(inv_list: list) -> tuple[list[str], Decimal]:
            ordered = sorted(
                inv_list,
                key=lambda i: (i.due_date, getattr(i, 'pk', 0) or 0),
            )
            newest = ordered[-1]
            lines: list[str] = []
            total = Decimal('0')
            for inv in ordered:
                bal = getattr(inv, 'computed_balance', None)
                if bal is None:
                    bal = inv.balance
                total += bal
                arrears = len(ordered) > 1 and inv.pk != newest.pk
                tag = f'{inv.term} (arrears)' if arrears else inv.term
                lines.append(
                    f'• *{tag}*: {format_kes(bal)} — due {format_date_human(inv.due_date)}'
                )
            return lines, total

        if len(entries) == 1:
            student = entries[0]['student']
            term_lines, total = _term_lines(entries[0]['invoices'])
            lines = [
                f'Hi {first}, here is the fee balance for *{student.full_name}* '
                f'(Adm {student.admission_number}) at *{school_name}*:',
                '',
                *term_lines,
                '',
                f'*Total outstanding: {format_kes(total)}*',
            ]
            if paybill:
                lines.append(f'\nPaybill: *{paybill}* · Account: *{student.admission_number}*')
            lines.append(
                '\nHow would you like to pay?\n'
                '*1* — M-Pesa now (full amount)\n'
                '*2* — Pay partially by M-Pesa\n'
                '*3* — Bring cash to the school\n'
                '*4* — I\'ll pay later (suggest a date)\n'
                '*5* — Talk to the school\n\n'
                'You can also reply naturally, e.g. *I will pay tomorrow via M-Pesa*.\n'
                'Reply *STOP* to pause reminders.'
            )
            return '\n'.join(lines)

        # Multiple children on the same parent phone
        grand_total = Decimal('0')
        lines = [
            f'Hi {first}, you have outstanding fees at *{school_name}* '
            f'for *{len(entries)} children*:',
            '',
        ]
        for index, entry in enumerate(entries, start=1):
            student = entry['student']
            term_lines, total = _term_lines(entry['invoices'])
            grand_total += total
            lines.append(
                f'*{index}. {student.full_name}* (Adm {student.admission_number})'
            )
            lines.extend(term_lines)
            lines.append(f'Total for {student.first_name}: *{format_kes(total)}*')
            lines.append('')

        lines.append(f'*Combined total: {format_kes(grand_total)}*')
        lines.append('')
        lines.append(
            'Reply with the *number* of the child you want to pay for '
            f'(e.g. *1* for {entries[0]["student"].first_name}), '
            'or reply with their admission number.\n\n'
            'After you choose, I will share pay options for that child only.\n'
            'Reply *STOP* to pause reminders.'
        )
        return '\n'.join(lines)
