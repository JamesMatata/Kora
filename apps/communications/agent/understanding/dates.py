"""
Date NLP for parent WhatsApp (rules-first, Kitabu-style).

Understands tomorrow/kesho/Friday/etc. Never invents ledger amounts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from django.utils import timezone

Confidence = Literal['high', 'medium', 'low', 'none']

_TYPO_MAP = {
    'tommorow': 'tomorrow',
    'tomorow': 'tomorrow',
    'tmrw': 'tomorrow',
    'tmr': 'tomorrow',
    'todays': 'today',
    'keso': 'kesho',
    'ksho': 'kesho',
}

_WEEKDAYS = {
    'monday': 0,
    'mon': 0,
    'tuesday': 1,
    'tue': 1,
    'tues': 1,
    'wednesday': 2,
    'wed': 2,
    'thursday': 3,
    'thu': 3,
    'thur': 3,
    'thurs': 3,
    'friday': 4,
    'fri': 4,
    'saturday': 5,
    'sat': 5,
    'sunday': 6,
    'sun': 6,
    'jumatatu': 0,
    'jumanne': 1,
    'jumatano': 2,
    'alhamisi': 3,
    'ijumaa': 4,
    'jumaa': 4,
    'jumamosi': 5,
    'jumapili': 6,
}


@dataclass
class DateParseResult:
    value: date | None
    confidence: Confidence
    ambiguous: bool = False
    raw: str = ''
    normalized: str = ''
    source: str = 'none'
    needs_confirmation: bool = False
    reason: str = ''

    @property
    def is_ok(self) -> bool:
        return (
            self.value is not None
            and self.confidence in {'high', 'medium'}
            and not self.needs_confirmation
            and not self.ambiguous
        )


def _normalize_text(raw: str) -> str:
    text = re.sub(r'\s+', ' ', (raw or '').strip().lower())
    text = re.sub(r"[^\w\s/.\-']", ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return ' '.join(_TYPO_MAP.get(tok, tok) for tok in text.split())


def _next_weekday(today: date, weekday: int, *, next_week: bool = False) -> date:
    days_ahead = (weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    if next_week and days_ahead < 7:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def parse_date_expression(
    raw: str,
    *,
    today: date | None = None,
) -> DateParseResult:
    today = today or timezone.localdate()
    normalized = _normalize_text(raw)
    text = normalized
    if not text:
        return DateParseResult(None, 'none', raw=raw or '', reason='empty')

    if re.search(r'\b(today|leo)\b', text):
        return DateParseResult(today, 'high', raw=raw, normalized=text, source='rules')
    if re.search(r'\b(evening|tonight|this evening|jioni|leo jioni)\b', text):
        return DateParseResult(
            today, 'high', raw=raw, normalized=text, source='rules', reason='evening'
        )
    if re.search(r'\b(tomorrow|kesho)\b', text) and 'kutwa' not in text:
        return DateParseResult(
            today + timedelta(days=1), 'high', raw=raw, normalized=text, source='rules'
        )
    if 'kesho kutwa' in text or 'day after tomorrow' in text:
        return DateParseResult(
            today + timedelta(days=2), 'high', raw=raw, normalized=text, source='rules'
        )
    if re.search(r'\b(next week|wiki ijayo)\b', text):
        return DateParseResult(
            today + timedelta(days=7), 'high', raw=raw, normalized=text, source='rules'
        )

    m = re.search(r'\b(?:in|after)\s+(\d+)\s+days?\b', text)
    if m:
        return DateParseResult(
            today + timedelta(days=int(m.group(1))),
            'high',
            raw=raw,
            normalized=text,
            source='rules',
        )

    for name, wd in _WEEKDAYS.items():
        if re.search(rf'\bnext\s+{name}\b', text):
            return DateParseResult(
                _next_weekday(today, wd, next_week=True),
                'high',
                raw=raw,
                normalized=text,
                source='rules',
            )
        if re.search(rf'\b{name}\b', text):
            return DateParseResult(
                _next_weekday(today, wd),
                'high',
                raw=raw,
                normalized=text,
                source='rules',
            )

    m = re.search(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b', text)
    if m:
        try:
            value = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return DateParseResult(value, 'high', raw=raw, normalized=text, source='rules')
        except ValueError:
            pass

    m = re.search(r'\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b', text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            value = date(y, mo, d)
            return DateParseResult(value, 'high', raw=raw, normalized=text, source='rules')
        except ValueError:
            pass

    return DateParseResult(None, 'none', raw=raw, normalized=text, source='rules', reason='unparsed')
