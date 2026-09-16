"""Shared setup checklist for Get started / Overview hub."""

from __future__ import annotations

from django.urls import reverse

from academics.models import AcademicYear, ClassStream, Student
from finance.models import FeeInvoice


def build_setup_checklist(school) -> dict:
    has_year = AcademicYear.objects.filter(school=school).exists()
    has_stream = ClassStream.objects.filter(school=school).exists()
    has_students = Student.objects.filter(school=school, is_active=True).exists()
    has_paybill = bool((school.paybill_number or '').strip())
    has_mpesa = bool(
        school.mpesa_consumer_key
        or school.mpesa_consumer_secret
        or school.mpesa_passkey
    )
    has_invoices = FeeInvoice.objects.filter(school=school).exists()
    has_twilio = bool((school.twilio_phone_number or '').strip())
    steps = [
        {
            'title': 'Add academic year',
            'done': has_year,
            'url': reverse('dashboard:school_settings'),
        },
        {
            'title': 'Create classes / streams',
            'done': has_stream,
            'url': reverse('dashboard:classes'),
        },
        {
            'title': 'Import or add students',
            'done': has_students,
            'url': reverse('dashboard:classes'),
        },
        {
            'title': 'Save Paybill number',
            'done': has_paybill,
            'url': reverse('dashboard:school_settings'),
        },
        {
            'title': 'Save M-Pesa API keys',
            'done': has_mpesa,
            'url': reverse('dashboard:school_settings'),
        },
        {
            'title': 'Create fee plan & generate invoices',
            'done': has_invoices,
            'url': reverse('finance:term_fee_plans'),
        },
        {
            'title': 'Optional: school WhatsApp sender',
            'done': has_twilio,
            'url': reverse('dashboard:school_settings'),
            'optional': True,
        },
    ]
    required = [s for s in steps if not s.get('optional')]
    done_count = sum(1 for s in required if s['done'])
    return {
        'steps': steps,
        'done_count': done_count,
        'total_required': len(required),
        'complete': done_count >= len(required),
    }
