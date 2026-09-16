"""
Context-aware reply helpers (Kitabu-style): main menu / back, etc.
"""

from __future__ import annotations

import re

AwaitingKind = str  # '', 'menu', 'partial_amount', 'promise_date'


def normalize_inbound_text(raw: str) -> str:
    text = re.sub(r'\s+', ' ', (raw or '').strip())
    return re.sub(r'^[*_~`]+|[*_~`]+$', '', text).strip()


def _norm(raw: str) -> str:
    text = normalize_inbound_text(raw).lower()
    return text.replace('’', "'")


def _has_any(text: str, *phrases: str) -> bool:
    return any(p in text for p in phrases)


def wants_main_menu(raw: str) -> bool:
    """True when the parent wants the payment menu / to go back / cancel."""
    text = _norm(raw)
    if not text:
        return False

    if text in {
        'menu',
        'back',
        'rudi',
        'options',
        'cancel',
        'start over',
        '0',
        'home',
        'main menu',
        'chaguo',
        'restart',
        'start again',
        'go back',
        'previous',
    }:
        return True

    if _has_any(
        text,
        'main menu',
        'show menu',
        'the menu',
        'need menu',
        'see menu',
        'go back',
        'take me back',
        'back to menu',
        'return to menu',
        'previous menu',
        'other options',
        'payment options',
        'my options',
        'change option',
        'change my option',
        'switch option',
        'different option',
        'another option',
        'change choice',
        'pick again',
        'start over',
        'start again',
        'cancel this',
        'never mind',
        'nevermind',
        'rudi nyuma',
        'rudi menu',
        'menu kuu',
        'nataka menu',
        'what are my options',
        'what can i do',
    ):
        return True

    return bool(
        re.search(
            r'\b(go back|change (my )?(mind|option|choice)|back to (the )?menu)\b',
            text,
        )
    )


def payment_menu_text(*, student_name: str = '') -> str:
    who = f' for *{student_name}*' if student_name else ''
    return (
        f'How would you like to pay{who}?\n'
        '1 — M-Pesa now (full amount)\n'
        '2 — Pay partially by M-Pesa\n'
        '3 — Bring cash to the school\n'
        '4 — I\'ll pay later (suggest a date)\n'
        '5 — Talk to the school\n\n'
        'You can also reply naturally, e.g. I will pay tomorrow via M-Pesa.\n'
        'Reply *menu* anytime to see these options again.'
    )
