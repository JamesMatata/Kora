"""
Prepare a Greenfields demo student + optional fee-reminder send for agent testing.

Twilio WhatsApp Sandbox in some regions (e.g. Kenya) joins as a peer id like
KE.2117397715508204 — not as +254…. Outbound must use that peer id or Twilio
returns error 63015. This command stores both the roster E.164 and the peer id.

Examples:
  .venv\\Scripts\\python.exe manage.py prepare_agent_test --phone=+254743113141 --remind
  .venv\\Scripts\\python.exe manage.py prepare_agent_test --phone=+254743113141 --peer=KE.2117397715508204 --remind
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from academics.models import ClassStream, GradeLevel, Student
from communications.models import ConversationSession, MessageLog, ParentContact
from communications.services.identity import confirm_session_admission
from communications.services.twilio_service import (
    _normalize_e164,
    get_twilio_client,
    is_sandbox_peer_id,
    normalize_whatsapp_identity,
)
from finance.models import FeeInvoice, PaymentPromise
from finance.services.reminder_service import REMINDER_BODY_MARKER, FeeReminderService
from tenants.models import School

SCHOOL_CODE = 'greenfields-academy'
ADMISSION = 'GF-AGENT-001'
TWILIO_SANDBOX = '+14155238886'
DEFAULT_PEER = 'KE.2117397715508204'


def _detect_sandbox_peer() -> str | None:
    """Best-effort: latest inbound WhatsApp From that looks like a sandbox peer."""
    try:
        client = get_twilio_client()
        for msg in client.messages.list(limit=30):
            raw = (msg.from_ or '').strip()
            if not raw.lower().startswith('whatsapp:'):
                continue
            ident = raw.split(':', 1)[1]
            if is_sandbox_peer_id(ident):
                return normalize_whatsapp_identity(ident)
    except Exception:
        return None
    return None


class Command(BaseCommand):
    help = (
        'Seed a realistic Greenfields test student (multi-term arrears), attach your '
        'Twilio Sandbox peer id for delivery, and optionally send a fee reminder.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--phone',
            required=True,
            help='Roster / M-Pesa E.164, e.g. +254743113141.',
        )
        parser.add_argument(
            '--peer',
            default='',
            help=(
                'Twilio Sandbox peer id (e.g. KE.2117397715508204). '
                'Auto-detected from recent Twilio inbound if omitted.'
            ),
        )
        parser.add_argument(
            '--remind',
            action='store_true',
            help='Send an overdue fee reminder WhatsApp for this parent (force).',
        )
        parser.add_argument(
            '--inbound-only',
            action='store_true',
            help=(
                'Clear admission confirmation so the next inbound message '
                'must re-confirm the admission number.'
            ),
        )
        parser.add_argument(
            '--admission',
            default=ADMISSION,
            help=f'Admission number to use (default {ADMISSION}).',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        raw_phone = (options.get('phone') or '').strip()
        try:
            phone = _normalize_e164(raw_phone)
        except ValueError as exc:
            raise CommandError(f'Invalid --phone: {exc}') from exc

        if phone == TWILIO_SANDBOX:
            raise CommandError(
                f'{TWILIO_SANDBOX} is the Twilio sandbox *sender*, not your phone. '
                'Use your +254 number for --phone and KE.… for --peer.'
            )

        raw_peer = (options.get('peer') or '').strip()
        if raw_peer:
            try:
                peer = normalize_whatsapp_identity(raw_peer)
            except ValueError as exc:
                raise CommandError(f'Invalid --peer: {exc}') from exc
            if not is_sandbox_peer_id(peer):
                raise CommandError('--peer must look like KE.2117397715508204')
        else:
            peer = _detect_sandbox_peer() or DEFAULT_PEER

        admission = (options.get('admission') or ADMISSION).strip()
        school = School.objects.filter(code=SCHOOL_CODE, is_active=True).first()
        if school is None:
            raise CommandError(f'School {SCHOOL_CODE} not found or inactive.')

        grade = (
            GradeLevel.objects.filter(school=school, name__iexact='Grade 6').first()
            or GradeLevel.objects.filter(school=school).order_by('order', 'name').first()
        )
        if grade is None:
            raise CommandError('Greenfields has no grade levels — seed academics first.')

        stream = (
            ClassStream.objects.filter(school=school, grade_level=grade)
            .order_by('name')
            .first()
        )
        if stream is None:
            raise CommandError(f'No stream found for {grade}.')

        student, _created = Student.objects.update_or_create(
            school=school,
            admission_number=admission,
            defaults={
                'first_name': 'Amina',
                'last_name': 'Wanjiru',
                'grade_level': grade,
                'current_stream': stream,
                'parent_name': 'Jane Wanjiru',
                'parent_phone': phone,
                'is_active': True,
            },
        )

        Student.objects.filter(school=school, parent_phone=phone).exclude(
            pk=student.pk
        ).update(parent_phone='+254700000000')

        today = timezone.localdate()
        specs = [
            {
                'term': 'Term 1 2026',
                'total': Decimal('18500.00'),
                'paid': Decimal('10000.00'),
                'discount': Decimal('0.00'),
                'due': today - timedelta(days=95),
                'note': '',
            },
            {
                'term': 'Term 2 2026',
                'total': Decimal('19200.00'),
                'paid': Decimal('5000.00'),
                'discount': Decimal('2000.00'),
                'due': today - timedelta(days=12),
                'note': 'Partial bursary — needy student fund',
            },
        ]

        invoices = []
        for spec in specs:
            inv, _ = FeeInvoice.objects.update_or_create(
                school=school,
                student=student,
                term=spec['term'],
                defaults={
                    'total_amount': spec['total'],
                    'paid_amount': spec['paid'],
                    'discount_amount': spec['discount'],
                    'discount_note': spec['note'],
                    'due_date': spec['due'],
                    'last_contacted_at': None,
                },
            )
            inv.refresh_status(save=True)
            invoices.append(inv)

        PaymentPromise.objects.filter(
            school=school,
            invoice__student=student,
            status=PaymentPromise.Status.PENDING,
        ).update(status=PaymentPromise.Status.BROKEN)

        # Clear any leftover synthetic peer contacts for this peer.
        ParentContact.objects.filter(
            school=school,
            whatsapp_peer_id__iexact=peer,
        ).exclude(phone_number=phone).delete()

        parent, _ = ParentContact.objects.update_or_create(
            school=school,
            phone_number=phone,
            defaults={
                'parent_name': 'Jane Wanjiru',
                'is_verified': True,
                'last_contacted_at': None,
                'reminders_paused_at': None,
                'reminders_paused_reason': '',
                'whatsapp_peer_id': peer,
            },
        )
        parent.students.set([student])

        MessageLog.objects.filter(
            school=school,
            session__parent_contact=parent,
            direction=MessageLog.Direction.OUTBOUND,
            sender=MessageLog.Sender.BOT,
            body__contains=REMINDER_BODY_MARKER,
        ).delete()

        ConversationSession.objects.filter(
            school=school,
            parent_contact=parent,
        ).exclude(status=ConversationSession.Status.CLOSED).update(
            status=ConversationSession.Status.CLOSED,
        )
        session = ConversationSession.objects.create(
            school=school,
            parent_contact=parent,
            status=ConversationSession.Status.BOT_ACTIVE,
            active_student=student,
        )

        inbound_only = bool(options.get('inbound_only'))
        if inbound_only:
            session.active_student = None
            session.admission_confirmed_at = None
            session.save(
                update_fields=['active_student', 'admission_confirmed_at', 'last_message_at']
            )
        else:
            confirm_session_admission(session, student)

        total_bal = sum((inv.balance for inv in invoices), Decimal('0'))

        if options.get('remind'):
            result = FeeReminderService().dispatch_overdue_reminders(school, force=True)
            if result.reminded:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'OK — {admission} ready (KES {total_bal:,.0f} due). Reminder sent.'
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f'OK — {admission} ready (KES {total_bal:,.0f} due). Reminder not sent.'
                    )
                )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'OK — {admission} ready (KES {total_bal:,.0f} due).'
                )
            )
