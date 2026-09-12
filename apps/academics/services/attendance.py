"""Attendance parent notification helpers."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_absent_alert_body(school, student, attendance_date, status_label: str) -> str:
    date_label = attendance_date.strftime('%d %b %Y')
    return (
        f'*{school.name} — Attendance alert*\n\n'
        f'Student: {student.full_name}\n'
        f'Admission: {student.admission_number}\n'
        f'Date: {date_label}\n'
        f'Status: {status_label}\n\n'
        'Please contact the school if this was unexpected.'
    )


def notify_absent_parents(school, records, *, force: bool = False) -> tuple[int, int]:
    """
    Daily ABSENT/LATE WhatsApp alerts are disabled.

    Parent attendance messaging now runs through weekly teacher-initiated updates
    (honor STOP). Kept as a no-op so older call sites stay safe.
    """
    logger.info(
        'Daily absent/late WhatsApp skipped (disabled) school=%s records=%s force=%s',
        getattr(school, 'pk', None),
        len(records or []),
        force,
    )
    return 0, len(records or [])
