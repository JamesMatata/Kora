"""
Compound fee-collection intent for parent WhatsApp (rules-first).

Respects conversation turn state: while awaiting a partial amount, lone
digits 1–5 are amounts — not menu choices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from communications.agent.understanding.dates import DateParseResult, parse_date_expression
from communications.agent.understanding.replies import wants_main_menu

ActionKind = Literal[
    'pay_now_mpesa',
    'pay_partial',
    'pay_later',
    'bring_cash',
    'talk_to_school',
    'show_menu',
    'amount_only',
    'unclear',
]


@dataclass
class FeeIntent:
    action: ActionKind
    date_result: DateParseResult | None = None
    amount: float | None = None
    date_uncertain: bool = False
    confidence: Literal['high', 'medium', 'low'] = 'high'
    raw: str = ''
    signals: list[str] = field(default_factory=list)


def extract_menu_digit(raw: str) -> str | None:
    """
    Return '1'..'5' when the parent clearly picked a reminder/menu option.

    Accepts plain digits and light WhatsApp formatting (*5*, 5., option 5).
    """
    text = re.sub(r'\s+', ' ', (raw or '').strip().lower())
    text = re.sub(r'^[*_~`]+|[*_~`]+$', '', text).strip()
    text = text.replace('’', "'")
    if not text:
        return None
    if re.fullmatch(r'[1-5]', text):
        return text
    m = re.fullmatch(
        r'(?:option|choice|number|chaguo|reply)?\s*([1-5])(?:\s*[.)\-—])?',
        text,
    )
    if m:
        return m.group(1)
    return None


def _norm(raw: str) -> str:
    text = re.sub(r'\s+', ' ', (raw or '').strip().lower())
    return text.replace('tommorow', 'tomorrow').replace('tomorow', 'tomorrow')



def _has_any(text: str, *needles: str) -> bool:
    return any(n in text for n in needles)


def is_uncertain_date_phrase(raw: str) -> bool:
    text = _norm(raw)
    if not text:
        return False
    if text in {
        'not sure',
        'unsure',
        'sijui',
        'idk',
        'no idea',
        'maybe',
        'later',
        'baadaye',
    }:
        return True
    return _has_any(
        text,
        'not sure',
        'not certain',
        'no idea',
        "don't know",
        'dont know',
        'do not know',
        'sijui',
        'siwezi sema',
        'hajui',
        'not decided',
        'no date',
        'any day',
        'whenever',
        'soon',
        'hakuna tarehe',
    )


_PAY_LATER_RE = re.compile(
    r'\b('
    r'will pay|i will pay|i\'ll pay|ill pay|going to pay|gonna pay|'
    r'will send|i will send|i\'ll send|'
    r'pay later|pay tomorrow|pay next|pay on |pay friday|pay monday|'
    r'cannot pay today|can\'t pay today|cant pay today|not today|'
    r'no money today|need more time|give me time|'
    r'nitalipa|nita lipa|baadaye|kesho|'
    r'in the evening|this evening|tonight|jioni|leo jioni'
    r')\b',
    re.IGNORECASE,
)


def _extract_amount(text: str, *, allow_small: bool = False) -> float | None:
    m = re.search(
        r'(?:kes|ksh|kshs\.?|sh\.?|shillings?)?\s*'
        r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)',
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    raw = m.group(1).replace(',', '')
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    # Outside amount-prompt context, ignore lone menu digits 1–9.
    if (
        not allow_small
        and value < 10
        and not re.search(r'\b(kes|ksh|sh)\b', text, re.I)
        and re.fullmatch(r'[1-9]', text.strip())
    ):
        return None
    if not allow_small and value < 10 and not re.search(r'\b(kes|ksh|sh)\b', text, re.I):
        # Still allow "500" etc.; only block tiny values that look like menu noise
        # when they appear as a lone digit (handled above). Multi-digit <10 is rare.
        if re.fullmatch(r'\d+(\.\d+)?', text.strip()) and value < 10:
            return None
    return value


def parse_fee_intent(raw: str, *, awaiting: str = '') -> FeeIntent:
    """
    Parse parent text.

    awaiting:
      - 'partial_amount': digits are payment amounts (even 1–5)
      - 'promise_date': prefer date / pay_later; digits are not menu
      - otherwise: 1–5 map to the reminder menu
    """
    text = _norm(raw)
    if not text:
        return FeeIntent('unclear', confidence='low', raw=raw or '')

    if wants_main_menu(raw):
        return FeeIntent('show_menu', raw=raw, signals=['menu_return'])

    awaiting = (awaiting or '').strip().lower()

    # Inside "how much?" — never treat 1–5 as menu options.
    if awaiting == 'partial_amount':
        if re.fullmatch(r'\d+(\.\d+)?', text.strip()):
            amount = float(text.strip())
            if amount > 0:
                return FeeIntent(
                    'amount_only',
                    amount=amount,
                    confidence='high',
                    raw=raw,
                    signals=['awaiting_amount'],
                )
        amount = _extract_amount(text, allow_small=True)
        if amount is not None:
            return FeeIntent(
                'amount_only',
                amount=amount,
                confidence='high',
                raw=raw,
                signals=['awaiting_amount'],
            )
        # Fall through for natural language ("a thousand", cancel already handled)

    # Awaiting a promise date — digits are not the main menu.
    if awaiting == 'promise_date':
        amount = _extract_amount(text, allow_small=False)
        if is_uncertain_date_phrase(raw):
            return FeeIntent(
                'pay_later',
                amount=amount,
                date_uncertain=True,
                confidence='high',
                raw=raw,
                signals=['awaiting_date', 'date_uncertain'],
            )
        date_result = parse_date_expression(raw)
        if date_result.is_ok or _PAY_LATER_RE.search(text) or date_result.value:
            return FeeIntent(
                'pay_later',
                date_result=date_result,
                amount=amount,
                confidence='high' if date_result.is_ok else 'medium',
                raw=raw,
                signals=['awaiting_date'],
            )
        # Bare number while awaiting date → ambiguous; re-prompt.
        return FeeIntent(
            'unclear',
            date_result=date_result,
            amount=amount,
            confidence='low',
            raw=raw,
            signals=['awaiting_date'],
        )

    # Reminder menu digits (only when not inside a sub-prompt)
    digit = extract_menu_digit(raw)
    if digit is not None:
        mapping = {
            '1': 'pay_now_mpesa',
            '2': 'pay_partial',
            '3': 'bring_cash',
            '4': 'pay_later',
            '5': 'talk_to_school',
        }
        return FeeIntent(
            mapping[digit],  # type: ignore[arg-type]
            confidence='high',
            raw=raw,
            signals=[f'menu_{digit}'],
        )

    if _has_any(
        text,
        'talk to the school',
        'speak to staff',
        'human',
        'bursar',
        'talk to someone',
    ):
        return FeeIntent('talk_to_school', raw=raw, signals=['escalate'])

    if _has_any(
        text, 'bring cash', 'pay cash', 'cash at school', 'nitakuja', 'come to school'
    ):
        return FeeIntent('bring_cash', raw=raw, signals=['cash'])

    amount = _extract_amount(text)
    date_result = parse_date_expression(raw)

    if _has_any(text, 'pay part', 'partial', 'part pay', 'pay some', 'half', 'nusu'):
        return FeeIntent(
            'pay_partial',
            date_result=date_result,
            amount=amount,
            raw=raw,
            signals=['partial'],
        )

    if _has_any(text, 'pay now', 'mpesa now', 'send stk', 'send prompt', 'lipa sasa'):
        return FeeIntent(
            'pay_now_mpesa',
            amount=amount,
            confidence='high',
            raw=raw,
            signals=['pay_now'],
        )

    pay_later = bool(_PAY_LATER_RE.search(text)) or (
        date_result.value is not None
        and _has_any(text, 'pay', 'lipa', 'send', 'nitalipa', 'will')
    )

    if is_uncertain_date_phrase(raw) and (
        pay_later or awaiting == 'promise_date' or _has_any(text, 'pay', 'rest', 'balance')
    ):
        return FeeIntent(
            'pay_later',
            amount=amount,
            date_uncertain=True,
            confidence='high',
            raw=raw,
            signals=['pay_later', 'date_uncertain'],
        )

    if pay_later:
        return FeeIntent(
            'pay_later',
            date_result=date_result,
            amount=amount,
            confidence='high' if date_result.is_ok else 'medium',
            raw=raw,
            signals=['pay_later'],
        )

    if _has_any(text, 'mpesa', 'm-pesa') and not pay_later:
        return FeeIntent('pay_now_mpesa', amount=amount, confidence='medium', raw=raw)

    # Lone multi-digit amount outside menu (e.g. "1000") — amount_only for context.
    if amount is not None and re.fullmatch(
        r'(?:kes|ksh|kshs\.?|sh\.?)?\s*\d[\d,]*(?:\.\d+)?',
        text.strip(),
        re.I,
    ):
        return FeeIntent('amount_only', amount=amount, confidence='medium', raw=raw)

    return FeeIntent(
        'unclear', amount=amount, date_result=date_result, confidence='low', raw=raw
    )
