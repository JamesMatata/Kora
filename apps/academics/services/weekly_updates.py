"""Weekly teacher → parent attendance + feedback WhatsApp updates."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from academics.models import (
    AttendanceRecord,
    ClassStream,
    Student,
    WeeklyParentUpdate,
    WeeklyStudentFeedback,
)
from communications.services.identity import (
    get_or_create_active_session,
    resolve_parent_identity,
)
from communications.services.twilio_service import send_whatsapp_message
from communications.services.whatsapp_delivery import local_now, local_today
from tenants.models import Notification
from tenants.services import notify_user

logger = logging.getLogger(__name__)


def current_week_monday(*, today=None):
    day = today or local_today()
    return day - timedelta(days=day.weekday())


def week_end_friday(week_start):
    return week_start + timedelta(days=4)


def is_weekly_update_window(*, now=None) -> bool:
    """Friday 16:00 through Sunday end (Africa/Nairobi) — nudge + send window."""
    current = local_now(now=now)
    if current.weekday() == 4 and current.hour >= 16:
        return True
    if current.weekday() in (5, 6):
        return True
    return False


def attendance_summary_for_student(school, student, week_start) -> str:
    week_end = week_end_friday(week_start)
    records = list(
        AttendanceRecord.objects.filter(
            school=school,
            student=student,
            date__gte=week_start,
            date__lte=week_end,
        ).order_by('date')
    )
    if not records:
        return 'No attendance marked this week.'

    counts = {key: 0 for key in AttendanceRecord.Status.values}
    absent_days = []
    late_days = []
    for rec in records:
        counts[rec.status] = counts.get(rec.status, 0) + 1
        if rec.status == AttendanceRecord.Status.ABSENT:
            absent_days.append(rec.date.strftime('%a'))
        elif rec.status == AttendanceRecord.Status.LATE:
            late_days.append(rec.date.strftime('%a'))

    present = counts.get(AttendanceRecord.Status.PRESENT, 0)
    late = counts.get(AttendanceRecord.Status.LATE, 0)
    absent = counts.get(AttendanceRecord.Status.ABSENT, 0)
    excused = counts.get(AttendanceRecord.Status.EXCUSED, 0)
    total = len(records)
    parts = [f'Present {present}/{total}']
    if late:
        day_bit = f' ({", ".join(late_days)})' if late_days else ''
        parts.append(f'Late {late}{day_bit}')
    if absent:
        day_bit = f' ({", ".join(absent_days)})' if absent_days else ''
        parts.append(f'Absent {absent}{day_bit}')
    if excused:
        parts.append(f'Excused {excused}')
    return ' · '.join(parts)


def get_or_create_weekly_update(school, stream, *, teacher, week_start=None):
    week_start = week_start or current_week_monday()
    update, _created = WeeklyParentUpdate.objects.get_or_create(
        school=school,
        stream=stream,
        week_start=week_start,
        defaults={
            'created_by': teacher,
            'status': WeeklyParentUpdate.Status.DRAFT,
        },
    )
    students = list(
        Student.objects.filter(
            school=school,
            current_stream=stream,
            is_active=True,
        ).order_by('admission_number')
    )
    existing = {
        line.student_id: line
        for line in update.feedback_lines.select_related('student')
    }
    for student in students:
        summary = attendance_summary_for_student(school, student, week_start)
        if student.pk in existing:
            line = existing[student.pk]
            if (
                line.attendance_summary != summary
                and update.status != WeeklyParentUpdate.Status.SENT
            ):
                line.attendance_summary = summary
                line.save(update_fields=['attendance_summary'])
            continue
        WeeklyStudentFeedback.objects.create(
            school=school,
            update=update,
            student=student,
            attendance_summary=summary,
        )
    return update


def polish_teacher_note(raw_note: str, *, student_name: str, school_name: str) -> str:
    """Clarify grammar only via Gemini; never invent facts."""
    note = (raw_note or '').strip()
    if not note:
        return ''
    api_key = (getattr(settings, 'GEMINI_API_KEY', '') or '').strip()
    if not api_key:
        return note
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        model = (
            getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash') or 'gemini-2.5-flash'
        )
        prompt = (
            'Rewrite the teacher note for a Kenyan school WhatsApp message to a parent. '
            'Keep the same meaning. Improve clarity and polite tone only. '
            'Do not invent behaviour, grades, or incidents. '
            'Keep it under 400 characters. No markdown headings.\n\n'
            f'School: {school_name}\nStudent: {student_name}\nNote:\n{note}'
        )
        response = client.models.generate_content(model=model, contents=prompt)
        text = (getattr(response, 'text', None) or '').strip()
        return text or note
    except Exception:
        logger.exception('Gemini note polish failed')
        return note


def build_parent_weekly_message(
    *,
    school,
    student,
    week_start,
    attendance_summary,
    note: str,
) -> str:
    first = (student.parent_name or 'Parent').strip().split()[0] or 'Parent'
    week_label = (
        week_start.strftime('%d %b')
        + ' – '
        + week_end_friday(week_start).strftime('%d %b %Y')
    )
    lines = [
        f'*{school.name} — weekly update*',
        '',
        f'Habari {first},',
        f'Here is the attendance update for *{student.full_name}* '
        f'(Adm {student.admission_number}) for the week of {week_label}:',
        '',
        attendance_summary or 'No attendance marked this week.',
        '',
    ]
    cleaned = (note or '').strip()
    if cleaned:
        lines.extend(['Teacher note:', cleaned, ''])
    else:
        lines.append('No additional comment from the class teacher this week.')
        lines.append('')
    lines.append('Reply to this chat if you need to talk to the school.')
    return '\n'.join(lines)


@transaction.atomic
def send_weekly_update(update: WeeklyParentUpdate, *, teacher) -> dict:
    """
    Send WhatsApp weekly updates for all students in the update.
    Honours parent STOP (reminders_paused_at).
    """
    if update.status == WeeklyParentUpdate.Status.SENT:
        return {'sent': 0, 'skipped': 0, 'already_sent': True}

    school = update.school
    sent = 0
    skipped = 0
    for line in update.feedback_lines.select_related('student').order_by(
        'student__admission_number'
    ):
        student = line.student
        phone = (student.parent_phone or '').strip()
        if not phone:
            line.skipped_reason = 'no_phone'
            line.save(update_fields=['skipped_reason'])
            skipped += 1
            continue
        try:
            parent = resolve_parent_identity(school, phone)
        except ValueError:
            line.skipped_reason = 'invalid_phone'
            line.save(update_fields=['skipped_reason'])
            skipped += 1
            continue
        if parent.reminders_paused_at is not None:
            line.skipped_reason = 'parent_stop'
            line.save(update_fields=['skipped_reason'])
            skipped += 1
            continue

        note = (line.polished_note or line.teacher_note or '').strip()
        body = build_parent_weekly_message(
            school=school,
            student=student,
            week_start=update.week_start,
            attendance_summary=line.attendance_summary,
            note=note,
        )
        session = get_or_create_active_session(school, parent)
        if session.active_student_id != student.pk:
            session.active_student = student
            session.save(update_fields=['active_student'])
        ok, _detail = send_whatsapp_message(
            school,
            parent.phone_number,
            body,
            session=session,
            sender_type='BOT',
        )
        if ok:
            line.message_body = body
            line.sent_at = timezone.now()
            line.skipped_reason = ''
            line.save(update_fields=['message_body', 'sent_at', 'skipped_reason'])
            sent += 1
        else:
            line.skipped_reason = 'send_failed'
            line.save(update_fields=['skipped_reason'])
            skipped += 1

    update.status = WeeklyParentUpdate.Status.SENT
    update.sent_at = timezone.now()
    update.sent_by = teacher
    update.save(update_fields=['status', 'sent_at', 'sent_by', 'updated_at'])
    return {'sent': sent, 'skipped': skipped, 'already_sent': False}


def streams_needing_weekly_update(school, teacher) -> list:
    """Streams assigned to teacher that have not sent this week's update."""
    if not is_weekly_update_window():
        return []
    week_start = current_week_monday()
    streams = list(
        ClassStream.objects.filter(
            school=school,
            class_teacher=teacher,
        ).select_related('grade_level')
    )
    needing = []
    for stream in streams:
        sent = WeeklyParentUpdate.objects.filter(
            school=school,
            stream=stream,
            week_start=week_start,
            status=WeeklyParentUpdate.Status.SENT,
        ).exists()
        if not sent:
            needing.append(stream)
    return needing


def remind_teachers_weekly_updates(*, school=None) -> dict:
    """
    Friday 4pm+ nudge: in-app notification + WhatsApp to teacher phone if set.
    """
    if not is_weekly_update_window():
        return {'reminded': 0, 'reason': 'outside_window'}

    week_start = current_week_monday()
    streams = ClassStream.objects.filter(class_teacher__isnull=False).select_related(
        'class_teacher',
        'school',
        'grade_level',
    )
    if school is not None:
        streams = streams.filter(school=school)

    reminded_teachers: set[int] = set()
    reminded = 0
    site = (getattr(settings, 'SITE_DOMAIN', '') or '').rstrip('/')

    for stream in streams:
        teacher = stream.class_teacher
        if teacher is None or teacher.pk in reminded_teachers:
            continue
        already = WeeklyParentUpdate.objects.filter(
            school=stream.school,
            stream=stream,
            week_start=week_start,
            status=WeeklyParentUpdate.Status.SENT,
        ).exists()
        if already:
            continue

        update = get_or_create_weekly_update(
            stream.school,
            stream,
            teacher=teacher,
            week_start=week_start,
        )
        if update.teacher_reminded_at and (
            timezone.now() - update.teacher_reminded_at
        ).total_seconds() < 6 * 3600:
            continue

        path = reverse(
            'dashboard:weekly_parent_update',
            kwargs={'stream_slug': stream.slug},
        )
        link = f'{site}{path}' if site else path
        notify_user(
            user=teacher,
            school=stream.school,
            kind=Notification.Kind.NOTICE,
            title='Weekly parent updates due',
            body=f'Please send the weekly attendance update for {stream}.',
            link=path,
        )

        membership = teacher.membership_for(stream.school)
        phone = (getattr(membership, 'phone_number', None) or '').strip()
        if phone:
            try:
                send_whatsapp_message(
                    stream.school,
                    phone,
                    (
                        f'*{stream.school.name}*\n\n'
                        f'Reminder: please send this week’s parent updates for '
                        f'*{stream}*.\n\n{link}'
                    ),
                    sender_type='BOT',
                )
            except Exception:
                logger.exception('Teacher weekly reminder WhatsApp failed')

        update.teacher_reminded_at = timezone.now()
        update.save(update_fields=['teacher_reminded_at', 'updated_at'])
        reminded_teachers.add(teacher.pk)
        reminded += 1

    return {'reminded': reminded, 'reason': 'ok'}
