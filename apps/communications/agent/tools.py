"""
Deterministic execution tools for the parent WhatsApp AI agent.

The model must not write to the database or trigger payments directly.
All side effects go through these typed, tenant-scoped tool functions.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Callable
from uuid import UUID

from django.conf import settings
from django.utils import timezone

from academics.models import AcademicYear, Student
from communications.models import ConversationSession, MessageLog
from finance.models import FeeInvoice, PaymentPromise
from finance.services.daraja import DarajaError, initiate_stk_push
from tenants.models import Notification, School, SchoolMembership
from tenants.services import notify_user

logger = logging.getLogger(__name__)

# Registry of tool callables for Gemini / Google ADK function calling.
TOOL_REGISTRY: dict[str, Callable[..., dict]] = {}


def agent_tool(fn: Callable[..., dict]) -> Callable[..., dict]:
    """
    Mark a function as an agent tool and register it for function calling.

    Compatible with Google ADK FunctionTool wrapping and Gemini
    function declarations derived from the function name + docstring +
    annotated parameters.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    wrapper._is_agent_tool = True  # type: ignore[attr-defined]
    wrapper.__tool_name__ = fn.__name__  # type: ignore[attr-defined]
    TOOL_REGISTRY[fn.__name__] = wrapper
    return wrapper


def get_agent_tools() -> list[Callable[..., dict]]:
    """Return registered tool callables (for ADK FunctionTool / Gemini)."""
    return list(TOOL_REGISTRY.values())


def as_adk_tools():
    """
    Wrap tools with google.adk.tools.FunctionTool when ADK is installed.

    Returns plain callables if ADK is unavailable so local wiring still works.
    """
    tools = get_agent_tools()
    try:
        from google.adk.tools import FunctionTool  # type: ignore
    except ImportError:
        return tools
    return [FunctionTool(func=tool) for tool in tools]


def _decimal(value: float | int | str | Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Invalid amount.') from exc


def _overpayment_allowance() -> Decimal:
    raw = getattr(settings, 'FEE_OVERPAYMENT_ALLOWANCE', 0)
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


def _require_school(school_id: str) -> School:
    if not school_id or not str(school_id).strip():
        raise ValueError('school_id is required.')
    try:
        UUID(str(school_id))
    except (TypeError, ValueError) as exc:
        raise ValueError('school_id must be a valid UUID.') from exc
    school = School.objects.filter(pk=school_id, is_active=True).first()
    if school is None:
        raise ValueError('School not found or inactive.')
    return school


def _require_student(school: School, admission_number: str) -> Student:
    admission = (admission_number or '').strip()
    if not admission:
        raise ValueError('admission_number is required.')
    student = (
        Student.objects.filter(
            school=school,
            admission_number__iexact=admission,
            is_active=True,
        )
        .select_related('grade_level', 'current_stream')
        .first()
    )
    if student is None:
        raise ValueError('Student not found for this school.')
    if student.school_id != school.id:
        raise ValueError('Student does not belong to this school.')
    return student


def _unpaid_invoices(school: School, student: Student) -> list[FeeInvoice]:
    """All non-paid invoices with a positive remaining balance, oldest due first."""
    invoices = list(
        FeeInvoice.objects.filter(school=school, student=student)
        .exclude(status=FeeInvoice.Status.PAID)
        .order_by('due_date', 'created_at')
    )
    return [inv for inv in invoices if inv.balance > 0]


def _current_term_invoice(school: School, student: Student) -> FeeInvoice | None:
    """
    Prefer an open invoice whose term references the current academic year;
    otherwise the newest unpaid invoice (latest due), then any latest invoice.
    """
    unpaid = _unpaid_invoices(school, student)
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    if year is not None:
        # Prefer the latest open term that matches the current year label.
        matches = [
            inv
            for inv in unpaid
            if year.name and year.name.casefold() in (inv.term or '').casefold()
        ]
        if matches:
            return max(matches, key=lambda inv: (inv.due_date, inv.created_at))
    if unpaid:
        return max(unpaid, key=lambda inv: (inv.due_date, inv.created_at))

    qs = FeeInvoice.objects.filter(school=school, student=student)
    return qs.order_by('-due_date', '-created_at').first()


def _term_closing_date(school: School, invoice: FeeInvoice) -> date:
    """
    Policy ceiling for payment promises.

    Arrears invoices often have past due dates; parents must still be able to
    promise 'tomorrow'. Ceiling = later of (latest unpaid due, today) + 90 days.
    """
    unpaid = _unpaid_invoices(school, invoice.student)
    dues = [inv.due_date for inv in unpaid] if unpaid else [invoice.due_date]
    latest_due = max(dues)
    today = timezone.localdate()
    return max(latest_due, today) + timedelta(days=90)


@agent_tool
def get_student_fee_balance(school_id: str, admission_number: str) -> dict:
    """
    Look up a student's fee balances across all unsettled terms (arrears).

    Args:
        school_id: Tenant school UUID.
        admission_number: Student admission number within that school.

    Returns:
        student_name, admission_number, invoices (per term), total_balance,
        due_date (ISO of earliest open due), status — or an error payload.
    """
    try:
        school = _require_school(school_id)
        student = _require_student(school, admission_number)
        unpaid = _unpaid_invoices(school, student)
        if not unpaid:
            latest = (
                FeeInvoice.objects.filter(school=school, student=student)
                .order_by('-due_date', '-created_at')
                .first()
            )
            if latest is None:
                return {
                    'success': False,
                    'error': 'No fee invoice found for this student.',
                    'student_name': student.full_name,
                    'admission_number': student.admission_number,
                }
            return {
                'success': True,
                'student_name': student.full_name,
                'admission_number': student.admission_number,
                'total_billed': float(latest.total_amount),
                'discount_amount': float(latest.discount_amount or 0),
                'discount_note': (latest.discount_note or '').strip(),
                'net_billed': float(latest.net_amount),
                'paid_amount': float(latest.paid_amount),
                'balance': 0.0,
                'total_balance': 0.0,
                'due_date': latest.due_date.isoformat(),
                'status': latest.status,
                'term': latest.term,
                'invoices': [],
                'message': (
                    f'No outstanding balance for {student.full_name}. '
                    f'Latest term ({latest.term}) is cleared.'
                ),
            }

        invoice_rows = []
        total_balance = Decimal('0.00')
        total_billed = Decimal('0.00')
        total_paid = Decimal('0.00')
        total_discount = Decimal('0.00')
        for inv in unpaid:
            bal = inv.balance
            total_balance += bal
            total_billed += inv.total_amount
            total_paid += inv.paid_amount
            total_discount += inv.discount_amount or Decimal('0.00')
            invoice_rows.append(
                {
                    'term': inv.term,
                    'total_billed': float(inv.total_amount),
                    'discount_amount': float(inv.discount_amount or 0),
                    'paid_amount': float(inv.paid_amount),
                    'balance': float(bal),
                    'due_date': inv.due_date.isoformat(),
                    'status': inv.status,
                }
            )

        primary = unpaid[0]
        lines = [
            f'*{student.full_name}* (Adm {student.admission_number})',
            f'Total outstanding: *{float(total_balance):.2f}*',
        ]
        if len(invoice_rows) == 1:
            row = invoice_rows[0]
            lines.append(
                f'{row["term"]}: {row["balance"]:.2f} due {row["due_date"]}.'
            )
        else:
            lines.append('By term:')
            for row in invoice_rows:
                lines.append(
                    f'- {row["term"]}: *{row["balance"]:.2f}* '
                    f'(due {row["due_date"]}, {row["status"]})'
                )

        return {
            'success': True,
            'student_name': student.full_name,
            'admission_number': student.admission_number,
            'total_billed': float(total_billed),
            'discount_amount': float(total_discount),
            'discount_note': '',
            'net_billed': float(total_billed - total_discount),
            'paid_amount': float(total_paid),
            'balance': float(total_balance),
            'total_balance': float(total_balance),
            'due_date': primary.due_date.isoformat(),
            'status': primary.status,
            'term': primary.term,
            'invoices': invoice_rows,
            'open_invoice_count': len(invoice_rows),
            'message': '\n'.join(lines),
        }
    except ValueError as exc:
        return {'success': False, 'error': str(exc)}
    except Exception:
        logger.exception('get_student_fee_balance failed')
        return {'success': False, 'error': 'Unable to fetch fee balance.'}


@agent_tool
def initiate_fee_payment(
    school_id: str,
    admission_number: str,
    phone_number: str,
    amount: float,
) -> dict:
    """
    Start an M-Pesa STK push for a student's outstanding fee balance.

    Args:
        school_id: Tenant school UUID.
        admission_number: Student admission number.
        phone_number: Payer MSISDN (E.164 or local Kenyan format).
        amount: Amount to collect (must be > 0 and within balance + allowance).

    Returns:
        success, checkout_request_id, amount, message.
    """
    try:
        school = _require_school(school_id)
        student = _require_student(school, admission_number)
        invoice = _current_term_invoice(school, student)
        if invoice is None:
            return {
                'success': False,
                'checkout_request_id': '',
                'amount': float(amount),
                'message': 'No fee invoice found for this student.',
            }
        if invoice.school_id != school.id:
            return {
                'success': False,
                'checkout_request_id': '',
                'amount': float(amount),
                'message': 'Tenant boundary violation.',
            }

        pay_amount = _decimal(amount)
        if pay_amount <= 0:
            return {
                'success': False,
                'checkout_request_id': '',
                'amount': float(pay_amount),
                'message': 'Amount must be greater than zero.',
            }

        # Overpayment is allowed: excess clears other open invoices then
        # becomes student fee credit (see finance.services.credits).
        balance = invoice.balance
        note_extra = ''
        if pay_amount > balance > 0:
            note_extra = (
                f' Amount is above this invoice balance ({float(balance):.2f}); '
                f'any excess will clear other fees or be held as credit.'
            )
        elif pay_amount > balance:
            note_extra = (
                ' This invoice is already cleared; the amount will be held as credit.'
            )

        ok, payment_tx, response_json = initiate_stk_push(
            school,
            invoice,
            phone_number,
            pay_amount,
        )
        checkout_id = payment_tx.checkout_request_id or ''
        if ok:
            return {
                'success': True,
                'checkout_request_id': checkout_id,
                'amount': float(pay_amount),
                'message': (
                    response_json.get('CustomerMessage')
                    or response_json.get('ResponseDescription')
                    or 'STK push sent. Ask the parent to enter their M-Pesa PIN.'
                )
                + note_extra,
            }
        return {
            'success': False,
            'checkout_request_id': checkout_id,
            'amount': float(pay_amount),
            'message': (
                payment_tx.result_desc
                or response_json.get('error')
                or 'Failed to initiate M-Pesa payment.'
            ),
        }
    except (ValueError, DarajaError) as exc:
        return {
            'success': False,
            'checkout_request_id': '',
            'amount': float(amount) if amount is not None else 0.0,
            'message': str(exc),
        }
    except Exception:
        logger.exception('initiate_fee_payment failed')
        return {
            'success': False,
            'checkout_request_id': '',
            'amount': float(amount) if amount is not None else 0.0,
            'message': 'Unable to initiate payment.',
        }


@agent_tool
def record_payment_promise(
    school_id: str,
    admission_number: str,
    promised_amount: float,
    promised_date: str = '',
) -> dict:
    """
    Record a PENDING payment promise against the student's current-term invoice.

    Args:
        school_id: Tenant school UUID.
        admission_number: Student admission number.
        promised_amount: Amount the parent commits to pay.
        promised_date: Natural date ("tomorrow", "Friday"), YYYY-MM-DD,
            or phrases like "not sure" / empty when the date is unknown.

    Returns:
        success, promised_amount, promised_date, date_uncertain, message.
    """
    try:
        school = _require_school(school_id)
        student = _require_student(school, admission_number)
        invoice = _current_term_invoice(school, student)
        if invoice is None:
            return {
                'success': False,
                'promised_amount': float(promised_amount),
                'promised_date': promised_date,
                'date_uncertain': False,
                'message': 'No fee invoice found for this student.',
            }
        if invoice.school_id != school.id:
            return {
                'success': False,
                'promised_amount': float(promised_amount),
                'promised_date': promised_date,
                'date_uncertain': False,
                'message': 'Tenant boundary violation.',
            }

        amount = _decimal(promised_amount)
        if amount <= 0:
            return {
                'success': False,
                'promised_amount': float(amount),
                'promised_date': promised_date,
                'date_uncertain': False,
                'message': 'Promised amount must be greater than zero.',
            }

        from communications.agent.understanding.dates import parse_date_expression
        from communications.agent.understanding.intent import is_uncertain_date_phrase

        date_raw = (promised_date or '').strip()
        uncertain = (not date_raw) or is_uncertain_date_phrase(date_raw)
        parsed_date = None

        if not uncertain:
            try:
                parsed_date = datetime.strptime(date_raw, '%Y-%m-%d').date()
            except ValueError:
                date_result = parse_date_expression(date_raw)
                if date_result.is_ok and date_result.value is not None:
                    parsed_date = date_result.value
                elif is_uncertain_date_phrase(date_raw):
                    uncertain = True
                else:
                    return {
                        'success': False,
                        'promised_amount': float(amount),
                        'promised_date': promised_date,
                        'date_uncertain': False,
                        'message': (
                            'Could not understand that date. '
                            'Try tomorrow, Friday, YYYY-MM-DD, or say not sure.'
                        ),
                    }

        if not uncertain and parsed_date is not None:
            today = timezone.localdate()
            closing = _term_closing_date(school, invoice)
            if parsed_date < today:
                return {
                    'success': False,
                    'promised_amount': float(amount),
                    'promised_date': parsed_date.isoformat(),
                    'date_uncertain': False,
                    'message': 'Promised date cannot be in the past.',
                }
            if parsed_date > closing:
                return {
                    'success': False,
                    'promised_amount': float(amount),
                    'promised_date': parsed_date.isoformat(),
                    'date_uncertain': False,
                    'message': (
                        f'Promised date is too far out '
                        f'(after {closing.isoformat()}). Please pick an earlier date.'
                    ),
                }

        promise = PaymentPromise.objects.create(
            school=school,
            invoice=invoice,
            promised_amount=amount,
            promised_date=None if uncertain else parsed_date,
            date_uncertain=uncertain,
            status=PaymentPromise.Status.PENDING,
        )
        when = (
            'date to be confirmed'
            if promise.date_uncertain or promise.promised_date is None
            else promise.promised_date.isoformat()
        )
        return {
            'success': True,
            'promised_amount': float(promise.promised_amount),
            'promised_date': (
                None
                if promise.promised_date is None
                else promise.promised_date.isoformat()
            ),
            'date_uncertain': promise.date_uncertain,
            'message': (
                f'Payment promise recorded for {student.full_name} '
                f'({when}).'
            ),
        }
    except ValueError as exc:
        return {
            'success': False,
            'promised_amount': float(promised_amount) if promised_amount else 0.0,
            'promised_date': promised_date,
            'date_uncertain': False,
            'message': str(exc),
        }
    except Exception:
        logger.exception('record_payment_promise failed')
        return {
            'success': False,
            'promised_amount': float(promised_amount) if promised_amount else 0.0,
            'promised_date': promised_date,
            'date_uncertain': False,
            'message': 'Unable to record payment promise.',
        }


@agent_tool
def notify_finance_staff(
    school_id: str,
    session_id: str,
    reason: str,
    title: str = 'Parent WhatsApp update',
) -> dict:
    """
    Notify finance staff without taking the bot offline.

    Use for cash-at-office intent. For a true human handoff, use
    flag_for_human_escalation instead.
    """
    try:
        school = _require_school(school_id)
        if not session_id or not str(session_id).strip():
            return {'success': False, 'notified': False, 'message': 'session_id is required.'}

        session = (
            ConversationSession.objects.select_related('parent_contact')
            .filter(pk=session_id)
            .first()
        )
        if session is None:
            return {'success': False, 'notified': False, 'message': 'Session not found.'}
        if session.school_id != school.id:
            return {
                'success': False,
                'notified': False,
                'message': 'Tenant boundary violation.',
            }

        reason_text = (reason or '').strip() or 'Parent update.'
        title_text = (title or '').strip() or 'Parent WhatsApp update'
        phone = session.parent_contact.phone_number

        MessageLog.objects.create(
            school=school,
            session=session,
            direction=MessageLog.Direction.OUTBOUND,
            sender=MessageLog.Sender.BOT,
            body=f'[NOTICE] {reason_text}',
            delivery_status=MessageLog.DeliveryStatus.SENT,
        )

        from communications.services.staff_notify import finance_staff_users
        from tenants.models import Notification
        from tenants.services import notify_user

        for user in finance_staff_users(school):
            notify_user(
                user=user,
                school=school,
                kind=Notification.Kind.NOTICE,
                title=title_text,
                body=f'{phone}: {reason_text}',
            )

        logger.info(
            'Notified finance staff session=%s school=%s reason=%r',
            session.pk,
            school.id,
            reason_text,
        )
        return {'success': True, 'notified': True}
    except ValueError as exc:
        return {'success': False, 'notified': False, 'message': str(exc)}
    except Exception:
        logger.exception('notify_finance_staff failed')
        return {'success': False, 'notified': False, 'message': 'Unable to notify staff.'}


@agent_tool
def flag_for_human_escalation(
    school_id: str,
    session_id: str,
    reason: str,
) -> dict:
    """
    Escalate a WhatsApp conversation to school staff.

    Args:
        school_id: Tenant school UUID.
        session_id: ConversationSession primary key.
        reason: Short reason for escalation (stored in the audit trail).

    Returns:
        success, escalated.
    """
    try:
        school = _require_school(school_id)
        if not session_id or not str(session_id).strip():
            return {'success': False, 'escalated': False, 'message': 'session_id is required.'}

        session = (
            ConversationSession.objects.select_related('parent_contact')
            .filter(pk=session_id)
            .first()
        )
        if session is None:
            return {'success': False, 'escalated': False, 'message': 'Session not found.'}
        if session.school_id != school.id:
            return {
                'success': False,
                'escalated': False,
                'message': 'Tenant boundary violation.',
            }

        reason_text = (reason or '').strip() or 'Parent requested human assistance.'
        session.status = ConversationSession.Status.ESCALATED_PENDING
        session.save(update_fields=['status', 'last_message_at'])

        # Conversation audit trail
        MessageLog.objects.create(
            school=school,
            session=session,
            direction=MessageLog.Direction.OUTBOUND,
            sender=MessageLog.Sender.BOT,
            body=f'[ESCALATION] {reason_text}',
            delivery_status=MessageLog.DeliveryStatus.SENT,
        )

        # Dashboard inbox for finance staff (admins + bursars)
        phone = session.parent_contact.phone_number
        from communications.services.staff_notify import finance_staff_users
        from tenants.models import Notification
        from tenants.services import notify_user

        for user in finance_staff_users(school):
            notify_user(
                user=user,
                school=school,
                kind=Notification.Kind.NOTICE,
                title='WhatsApp conversation escalated',
                body=f'{phone}: {reason_text}',
            )

        logger.info(
            'Escalated session=%s school=%s reason=%r',
            session.pk,
            school.id,
            reason_text,
        )
        return {'success': True, 'escalated': True}
    except ValueError as exc:
        return {'success': False, 'escalated': False, 'message': str(exc)}
    except Exception:
        logger.exception('flag_for_human_escalation failed')
        return {'success': False, 'escalated': False, 'message': 'Unable to escalate.'}


# Gemini / OpenAPI-style declarations for function calling (ADK-agnostic).
GEMINI_FUNCTION_DECLARATIONS: list[dict[str, Any]] = [
    {
        'name': 'get_student_fee_balance',
        'description': (
            'Look up fee balances across all unsettled terms (arrears included). '
            'Requires school_id and admission_number.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'school_id': {'type': 'string', 'description': 'School UUID'},
                'admission_number': {
                    'type': 'string',
                    'description': 'Student admission number',
                },
            },
            'required': ['school_id', 'admission_number'],
        },
    },
    {
        'name': 'initiate_fee_payment',
        'description': (
            'Initiate an M-Pesa STK push for school fees. '
            'Amount must be > 0 and within outstanding balance policy.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'school_id': {'type': 'string'},
                'admission_number': {'type': 'string'},
                'phone_number': {
                    'type': 'string',
                    'description': 'Payer phone in E.164 or local format',
                },
                'amount': {'type': 'number'},
            },
            'required': [
                'school_id',
                'admission_number',
                'phone_number',
                'amount',
            ],
        },
    },
    {
        'name': 'record_payment_promise',
        'description': (
            'Record a PENDING promise to pay. Date may be natural language '
            '(tomorrow, Friday), YYYY-MM-DD, or "not sure" when unknown.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'school_id': {'type': 'string'},
                'admission_number': {'type': 'string'},
                'promised_amount': {'type': 'number'},
                'promised_date': {
                    'type': 'string',
                    'description': (
                        'tomorrow / Friday / YYYY-MM-DD / not sure '
                        '(empty allowed when unsure)'
                    ),
                },
            },
            'required': [
                'school_id',
                'admission_number',
                'promised_amount',
            ],
        },
    },
    {
        'name': 'flag_for_human_escalation',
        'description': (
            'Hand the WhatsApp chat to school staff (true escalation). '
            'Use when the parent asks to talk to the school (*5*). '
            'Do NOT use for cash-at-office (*3*) — use notify_finance_staff instead.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'school_id': {'type': 'string'},
                'session_id': {'type': 'string'},
                'reason': {'type': 'string'},
            },
            'required': ['school_id', 'session_id', 'reason'],
        },
    },
    {
        'name': 'notify_finance_staff',
        'description': (
            'Notify bursar/admin without taking the bot offline. '
            'Use when the parent will bring cash to the office (*3*).'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'school_id': {'type': 'string'},
                'session_id': {'type': 'string'},
                'reason': {'type': 'string'},
                'title': {'type': 'string'},
            },
            'required': ['school_id', 'session_id', 'reason'],
        },
    },
]
