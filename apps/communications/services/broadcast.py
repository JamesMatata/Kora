"""Resolve recipients and dispatch school WhatsApp broadcast notices."""

from __future__ import annotations

import logging
import threading
import time

from django.db import close_old_connections

from academics.models import ClassStream, GradeLevel, Student
from communications.models import BroadcastNotice
from communications.services.twilio_service import (
    _normalize_e164,
    send_whatsapp_message,
)

logger = logging.getLogger(__name__)

SEND_DELAY_SECONDS = 0.1


def resolve_broadcast_phones(school, target_audience: str, target_id=None) -> list[str]:
    """
    Return unique E.164 parent phone numbers for the broadcast audience.
    """
    students = Student.objects.filter(school=school, is_active=True)
    audience = (target_audience or BroadcastNotice.TargetAudience.ALL_PARENTS).upper()

    if audience == BroadcastNotice.TargetAudience.GRADE_LEVEL:
        if not target_id:
            return []
        if not GradeLevel.objects.filter(school=school, pk=target_id).exists():
            return []
        students = students.filter(grade_level_id=target_id)
    elif audience == BroadcastNotice.TargetAudience.CLASS_STREAM:
        if not target_id:
            return []
        if not ClassStream.objects.filter(school=school, pk=target_id).exists():
            return []
        students = students.filter(current_stream_id=target_id)

    phones: set[str] = set()
    for raw in students.values_list('parent_phone', flat=True).iterator():
        value = (raw or '').strip()
        if not value:
            continue
        try:
            phones.add(_normalize_e164(value))
        except ValueError:
            continue
    return sorted(phones)


def format_broadcast_body(title: str, message: str, school_name: str) -> str:
    title = (title or '').strip()
    message = (message or '').strip()
    return (
        f'*{title}*\n\n'
        f'{message}\n\n'
        f'— {school_name}'
    )


def dispatch_broadcast_notice(notice_id: int, *, delay_seconds: float = SEND_DELAY_SECONDS) -> None:
    """
    Send a BroadcastNotice to all resolved parent phones sequentially.

    Rate-limits between Twilio calls. Safe to run in a background thread.
    """
    close_old_connections()
    try:
        notice = (
            BroadcastNotice.objects.select_related('school', 'created_by')
            .filter(pk=notice_id)
            .first()
        )
        if notice is None:
            return

        school = notice.school
        phones = resolve_broadcast_phones(
            school,
            notice.target_audience,
            notice.target_id,
        )
        body = format_broadcast_body(notice.title, notice.message, school.name)

        notice.total_recipients = len(phones)
        notice.sent_count = 0
        notice.status = BroadcastNotice.Status.SENDING
        notice.save(update_fields=['total_recipients', 'sent_count', 'status'])

        if not phones:
            notice.status = BroadcastNotice.Status.FAILED
            notice.save(update_fields=['status'])
            return

        sent = 0
        failed = 0
        for index, phone in enumerate(phones):
            ok, detail = send_whatsapp_message(
                school,
                phone,
                body,
                session=None,
                sender_type='BOT',
            )
            if ok:
                sent += 1
            else:
                failed += 1
                logger.warning(
                    'Broadcast %s failed phone=%s: %s',
                    notice.pk,
                    phone,
                    detail,
                )
            notice.sent_count = sent
            notice.save(update_fields=['sent_count'])
            if index < len(phones) - 1 and delay_seconds > 0:
                time.sleep(delay_seconds)

        if sent == 0:
            notice.status = BroadcastNotice.Status.FAILED
        else:
            notice.status = BroadcastNotice.Status.COMPLETED
        notice.save(update_fields=['status'])
    except Exception:
        logger.exception('Broadcast dispatch crashed notice_id=%s', notice_id)
        BroadcastNotice.objects.filter(pk=notice_id).update(
            status=BroadcastNotice.Status.FAILED,
        )
    finally:
        close_old_connections()


def enqueue_broadcast_notice(notice_id: int) -> None:
    """Start sequential WhatsApp dispatch in a daemon thread."""
    thread = threading.Thread(
        target=dispatch_broadcast_notice,
        args=(notice_id,),
        name=f'broadcast-{notice_id}',
        daemon=True,
    )
    thread.start()
