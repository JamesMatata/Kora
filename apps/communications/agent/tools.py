"""
Deterministic execution tools for the parent WhatsApp AI agent.

The model must not write to the database or trigger payments directly.
All side effects go through these typed, tenant-scoped tool functions.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
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


def _current_term_invoice(school: School, student: Student) -> FeeInvoice | None:
    """
    Prefer an open invoice whose term references the current academic year;
    otherwise the most recent non-paid invoice, then any latest invoice.
    """
    qs = FeeInvoice.objects.filter(school=school, student=student)
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    if year is not None:
        year_qs = qs.filter(term__icontains=year.name)
        open_for_year = (
            year_qs.exclude(status=FeeInvoice.Status.PAID)
            .order_by('-due_date', '-created_at')
            .first()
        )
        if open_for_year is not None:
            return open_for_year
        latest_year = year_qs.order_by('-due_date', '-created_at').first()
        if latest_year is not None:
            return latest_year

    open_invoice = (
        qs.exclude(status=FeeInvoice.Status.PAID)
        .order_by('-due_date', '-created_at')
        .first()
    )
    if open_invoice is not None:
        return open_invoice
    return qs.order_by('-due_date', '-created_at').first()


def _term_closing_date(school: School, invoice: FeeInvoice) -> date:
    """Policy ceiling for payment promises — invoice due date (term close)."""
    return invoice.due_date


@agent_tool
def get_student_fee_balance(school_id: str, admission_number: str) -> dict:
    """
    Look up a student's active fee invoice balance for the current term.

    Args:
        school_id: Tenant school UUID.
        admission_number: Student admission number within that school.

    Returns:
        student_name, admission_number, total_billed, paid_amount, balance,
        due_date (ISO), status — or an error payload.
    """
    try:
        school = _require_school(school_id)
        student = _require_student(school, admission_number)
        invoice = _current_term_invoice(school, student)
        if invoice is None:
            return {
                'success': False,
                'error': 'No fee invoice found for this student.',
                'student_name': student.full_name,
                'admission_number': student.admission_number,
            }
        if invoice.school_id != school.id or invoice.student_id != student.id:
            return {'success': False, 'error': 'Tenant boundary violation.'}

        balance = invoice.balance
        return {
            'student_name': student.full_name,
            'admission_number': student.admission_number,
            'total_billed': float(invoice.total_amount),
            'paid_amount': float(invoice.paid_amount),
            'balance': float(balance),
            'due_date': invoice.due_date.isoformat(),
            'status': invoice.status,
            'term': invoice.term,
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

        balance = invoice.balance
        allowance = _overpayment_allowance()
        max_allowed = balance + allowance
        if pay_amount > max_allowed:
            return {
                'success': False,
                'checkout_request_id': '',
                'amount': float(pay_amount),
                'message': (
                    f'Amount exceeds outstanding balance '
                    f'({float(balance):.2f}) plus allowed overpayment '
                    f'({float(allowance):.2f}).'
                ),
            }

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
                ),
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
    promised_date: str,
) -> dict:
    """
    Record a PENDING payment promise against the student's current-term invoice.

    Args:
        school_id: Tenant school UUID.
        admission_number: Student admission number.
        promised_amount: Amount the parent commits to pay.
        promised_date: Promise date in YYYY-MM-DD (not past, not after term close).

    Returns:
        success, promised_amount, promised_date, message.
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
                'message': 'No fee invoice found for this student.',
            }
        if invoice.school_id != school.id:
            return {
                'success': False,
                'promised_amount': float(promised_amount),
                'promised_date': promised_date,
                'message': 'Tenant boundary violation.',
            }

        amount = _decimal(promised_amount)
        if amount <= 0:
            return {
                'success': False,
                'promised_amount': float(amount),
                'promised_date': promised_date,
                'message': 'Promised amount must be greater than zero.',
            }

        try:
            parsed_date = datetime.strptime(
                (promised_date or '').strip(),
                '%Y-%m-%d',
            ).date()
        except ValueError:
            return {
                'success': False,
                'promised_amount': float(amount),
                'promised_date': promised_date,
                'message': 'promised_date must be YYYY-MM-DD.',
            }

        today = timezone.localdate()
        closing = _term_closing_date(school, invoice)
        if parsed_date < today:
            return {
                'success': False,
                'promised_amount': float(amount),
                'promised_date': parsed_date.isoformat(),
                'message': 'Promised date cannot be in the past.',
            }
        if parsed_date > closing:
            return {
                'success': False,
                'promised_amount': float(amount),
                'promised_date': parsed_date.isoformat(),
                'message': (
                    f'Promised date cannot be after the term closing date '
                    f'({closing.isoformat()}).'
                ),
            }

        promise = PaymentPromise.objects.create(
            school=school,
            invoice=invoice,
            promised_amount=amount,
            promised_date=parsed_date,
            status=PaymentPromise.Status.PENDING,
        )
        return {
            'success': True,
            'promised_amount': float(promise.promised_amount),
            'promised_date': promise.promised_date.isoformat(),
            'message': (
                f'Payment promise recorded for {student.full_name} '
                f'by {promise.promised_date.isoformat()}.'
            ),
        }
    except ValueError as exc:
        return {
            'success': False,
            'promised_amount': float(promised_amount) if promised_amount else 0.0,
            'promised_date': promised_date,
            'message': str(exc),
        }
    except Exception:
        logger.exception('record_payment_promise failed')
        return {
            'success': False,
            'promised_amount': float(promised_amount) if promised_amount else 0.0,
            'promised_date': promised_date,
            'message': 'Unable to record payment promise.',
        }


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

        # Dashboard inbox for admins
        phone = session.parent_contact.phone_number
        admin_ids = SchoolMembership.objects.filter(
            school=school,
            is_admin=True,
            user__is_active=True,
        ).values_list('user_id', flat=True)
        from django.contrib.auth import get_user_model

        for user in get_user_model().objects.filter(pk__in=admin_ids):
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
            'Look up a student fee balance for the current term. '
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
            'Record a PENDING promise to pay by a future date (YYYY-MM-DD), '
            'not past and not after term closing.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'school_id': {'type': 'string'},
                'admission_number': {'type': 'string'},
                'promised_amount': {'type': 'number'},
                'promised_date': {
                    'type': 'string',
                    'description': 'YYYY-MM-DD',
                },
            },
            'required': [
                'school_id',
                'admission_number',
                'promised_amount',
                'promised_date',
            ],
        },
    },
    {
        'name': 'flag_for_human_escalation',
        'description': (
            'Escalate the WhatsApp session to school staff and write an audit entry.'
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
]
