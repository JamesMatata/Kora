"""Outbound WhatsApp delivery helpers (Kitabu-style quiet hours + sanitize)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

# Paths, prompts, stack frames, JSON dumps, internal markers.
_FORBIDDEN = re.compile(
    r'('
    r'@[\w./\\-]+\.(?:txt|py|md|json|dart)|'
    r'(?:Traceback|File \".+\", line \d+)|'
    r'(?:system prompt|SYSTEM:|<<SYS>>)|'
    r'(?:```(?:json|python|bash)?)|'
    r'(?:\{[\'\"](?:status|error|detail|traceback)[\'\"])|'
    r'(?:As an AI|as a language model)'
    r')',
    re.IGNORECASE,
)
_MARKDOWN_HEAVY = re.compile(r'#{1,6}\s+|>{1,3}\s+|\[([^\]]+)\]\([^)]+\)')
_MULTI_SPACE = re.compile(r'[ \t]{2,}')
_MULTI_NL = re.compile(r'\n{3,}')


def whatsapp_local_tz() -> ZoneInfo:
    name = getattr(settings, 'WHATSAPP_LOCAL_TZ', 'Africa/Nairobi') or 'Africa/Nairobi'
    return ZoneInfo(name)


def local_now(*, now=None) -> datetime:
    aware = timezone.localtime(now) if now is not None else timezone.localtime()
    return aware.astimezone(whatsapp_local_tz())


def local_today(*, now=None):
    return local_now(now=now).date()


def local_week_start(*, now=None) -> datetime:
    """Monday 00:00 in the WhatsApp local timezone for the current week."""
    today = local_today(now=now)
    monday = today - timedelta(days=today.weekday())
    return datetime.combine(monday, datetime.min.time(), tzinfo=whatsapp_local_tz())


def format_kes(amount) -> str:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return 'KES 0'
    quantized = (
        value.quantize(Decimal('1'))
        if value == value.to_integral_value()
        else value.quantize(Decimal('0.01'))
    )
    if quantized == quantized.to_integral_value():
        return f'KES {int(quantized):,}'
    return f'KES {quantized:,.2f}'


def format_date_human(value) -> str:
    if value is None:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime('%d %b %Y')
    return str(value).strip()


def sanitize_outbound_text(text: str, *, max_len: int = 1200) -> str:
    """Strip unsafe / embarrassing content before WhatsApp send."""
    raw = (text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not raw:
        return ''

    if _FORBIDDEN.search(raw):
        kept = [line for line in raw.split('\n') if not _FORBIDDEN.search(line)]
        raw = '\n'.join(kept).strip()

    raw = _MARKDOWN_HEAVY.sub(r'\1', raw)
    raw = raw.replace('```', '').replace('`', '')
    raw = _MULTI_SPACE.sub(' ', raw)
    raw = _MULTI_NL.sub('\n\n', raw).strip()

    if len(raw) > max_len:
        raw = raw[: max_len - 1].rstrip() + '…'

    if len(raw) < 3:
        return ''
    if raw.startswith('{') and raw.endswith('}'):
        return ''
    if raw.startswith('[') and raw.endswith(']'):
        return ''
    return raw


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def quiet_windows(*, school=None) -> list[tuple[int, int]]:
    """
    Quiet-hour window(s) for fee reminders.

    Prefer per-school settings; fall back to platform env defaults.
    """
    if school is not None:
        start = int(getattr(school, 'reminder_quiet_hour_start', 20) or 20)
        end = int(getattr(school, 'reminder_quiet_hour_end', 8) or 8)
    else:
        start = int(getattr(settings, 'WHATSAPP_QUIET_HOUR_START', 20))
        end = int(getattr(settings, 'WHATSAPP_QUIET_HOUR_END', 8))
    start = max(0, min(23, start))
    end = max(0, min(23, end))
    return [(start, end)]


def in_quiet_period(*, now=None, school=None) -> bool:
    current = local_now(now=now)
    hour = current.hour
    for start, end in quiet_windows(school=school):
        if _hour_in_window(hour, start, end):
            return True
    return False


def next_delivery_at(*, now=None, school=None) -> datetime:
    """Next local datetime outside quiet hours (scan hour-by-hour, max 36h)."""
    current = local_now(now=now)
    candidate = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(36):
        if not in_quiet_period(now=candidate, school=school):
            return candidate
        candidate += timedelta(hours=1)
    return current + timedelta(hours=1)


def format_quiet_hours_label(*, school=None) -> str:
    """Human label like '8:00 pm – 8:00 am' for UI copy."""
    start, end = quiet_windows(school=school)[0]

    def _fmt(hour: int) -> str:
        suffix = 'am' if hour < 12 else 'pm'
        h12 = hour % 12 or 12
        return f'{h12}:00 {suffix}'

    return f'{_fmt(start)} – {_fmt(end)}'


def format_messaging_hours_label(*, school=None) -> str:
    """Inverse of quiet hours — when reminders may go out."""
    start, end = quiet_windows(school=school)[0]
    # Messaging allowed from end .. start (wrapping).
    def _fmt(hour: int) -> str:
        suffix = 'am' if hour < 12 else 'pm'
        h12 = hour % 12 or 12
        return f'{h12}:00 {suffix}'

    return f'{_fmt(end)} – {_fmt(start)}'


def contacted_on_local_date(last_contacted_at, *, day=None) -> bool:
    """True if last_contacted_at falls on the given Nairobi local calendar day."""
    if last_contacted_at is None:
        return False
    target = day or local_today()
    return local_now(now=last_contacted_at).date() == target
