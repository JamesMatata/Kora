"""
Kora conversational agent orchestrator (Gemini 2.5 Flash + tool calling).

Uses google-genai with automatic function calling against the deterministic
tools in communications.agent.tools. Side effects never bypass those tools.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from communications.models import MessageLog

logger = logging.getLogger(__name__)

MODEL_NAME = 'gemini-2.5-flash'
HISTORY_LIMIT = 10


def _build_system_instruction(*, school, student) -> str:
    school_name = school.name
    student_name = student.full_name
    admission = student.admission_number
    return f"""You are Kora, an efficient, polite school fee assistant for {school_name} on WhatsApp.

Active student:
- Name: {student_name}
- Admission: {admission}

Parents write naturally (English/Swahili mix, typos like "tommorow"). Understand intent — do not force rigid forms.

## Rules
1. Never invent balances, receipts, or payment confirmations. Use tools.
2. Call `get_student_fee_balance` when discussing money. Share term arrears + *total*.
3. Natural language:
   - "I will pay tomorrow" / "nitalipa kesho" / "Friday" → promise for that date. Default amount = *full outstanding total* unless they named another amount. Call `record_payment_promise` immediately. Do NOT ask for YYYY-MM-DD when the relative date is clear. Do NOT re-ask the amount if they did not specify a partial — use the total. If they say *not sure* / *sijui* / *don't know*, record the promise with date uncertain — do not force a calendar date.
   - After a partial payment receipt asking when they will clear the rest, accept a date or *not sure*.
   - "Pay now" / "M-Pesa" / reminder *1* → STK for the full total (confirm once briefly if helpful, then `initiate_fee_payment`).
   - Partial / *2* → ask how much only, then STK. While waiting for the amount, treat *any* number (including 1–5) as KES — never as a menu choice.
   - Cash / *3* → acknowledge office cash payment and *notify* staff (do NOT escalate / hand off).
   - Talk to school / *5* → `flag_for_human_escalation` only (true staff handoff). Never treat *5* as cash.
   - "menu" / "back" / "main menu" → re-show the payment options.
4. Reminder digits (only at the main menu, not inside a sub-prompt): *1* M-Pesa full, *2* partial, *3* cash (notify), *4* pay later, *5* talk to school (escalate). Digits must map exactly — never swap 3 and 5.
5. Keep replies short and WhatsApp-friendly (*bold* key amounts/dates).
6. Dates for `record_payment_promise` may be natural language ("tomorrow", "kesho", "Friday"), YYYY-MM-DD, or "not sure".
7. Outside fees/admin, escalate.

## Tools
Scoped to this school/student. Summarise tool results clearly for the parent.
"""


class KoraAgentOrchestrator:
    """Gemini-powered orchestrator for a single parent WhatsApp conversation."""

    def __init__(self, school, session, student):
        if school is None or session is None or student is None:
            raise ValueError('school, session, and student are required.')
        if session.school_id != school.id:
            raise ValueError('Session does not belong to this school.')
        if student.school_id != school.id:
            raise ValueError('Student does not belong to this school.')

        self.school = school
        self.session = session
        self.student = student
        self.model_name = getattr(
            settings, 'GEMINI_MODEL', MODEL_NAME
        ) or MODEL_NAME

        api_key = (getattr(settings, 'GEMINI_API_KEY', '') or '').strip()
        if not api_key:
            raise RuntimeError(
                'GEMINI_API_KEY is not configured. Set it in the environment.'
            )

        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._system_instruction = _build_system_instruction(
            school=school,
            student=student,
        )
        self._tools = self._build_bound_tools()

    def _stk_phone(self) -> str:
        """Prefer roster parent phone for Daraja STK (E.164), not sandbox peer ids."""
        roster = (getattr(self.student, 'parent_phone', None) or '').strip()
        if roster.startswith('+') and not roster.startswith('+299'):
            return roster
        return self.session.parent_contact.phone_number

    def _outstanding_total(self) -> float | None:
        from communications.agent import tools as agent_tools

        data = agent_tools.get_student_fee_balance(
            str(self.school.id),
            self.student.admission_number,
        )
        if not data:
            return None
        total = data.get('total_balance', data.get('balance'))
        try:
            return float(total)
        except (TypeError, ValueError):
            return None

    def _get_awaiting(self) -> str:
        last_bot = (
            MessageLog.objects.filter(
                school=self.school,
                session=self.session,
                sender=MessageLog.Sender.BOT,
            )
            .order_by('-created_at')
            .values_list('body', flat=True)
            .first()
        ) or ''
        lower = last_bot.lower()

        # A fresh pay menu / fee reminder always wins — digits are menu choices.
        # Do NOT match the menu line "4 — I'll pay later (suggest a date)".
        if 'how would you like to pay' in lower:
            if (self.session.agent_awaiting or '') != 'menu':
                self._set_awaiting('menu')
            return 'menu'

        awaiting = (getattr(self.session, 'agent_awaiting', None) or '').strip()
        if awaiting in {'partial_amount', 'promise_date', 'menu'}:
            return awaiting

        # Infer only from dedicated sub-prompts (not menu copy).
        if 'how much would you like to pay' in lower:
            return 'partial_amount'
        if 'what date can you pay by' in lower:
            return 'promise_date'
        return ''

    def _set_awaiting(self, value: str) -> None:
        value = (value or '').strip()
        if (self.session.agent_awaiting or '') == value:
            return
        self.session.agent_awaiting = value
        self.session.save(update_fields=['agent_awaiting'])

    def _payment_menu(self) -> str:
        from communications.agent.understanding.replies import payment_menu_text

        self._set_awaiting('menu')
        return payment_menu_text(student_name=self.student.full_name)

    def _handle_menu_digit(self, digit: str) -> str:
        """
        Absolute menu routing for reminder options 1–5.
        3 = cash (notify only). 5 = talk to school (true escalate). Never swap.
        """
        from communications.agent import tools as agent_tools

        school_id = str(self.school.id)
        admission = self.student.admission_number

        if digit == '1':
            amount = self._outstanding_total()
            if amount is None or amount <= 0:
                self._set_awaiting('')
                return (
                    f'There is no outstanding balance for *{self.student.full_name}* '
                    'right now.'
                )
            self._set_awaiting('')
            return self._start_stk(float(amount))

        if digit == '2':
            self._set_awaiting('partial_amount')
            return (
                'How much would you like to pay today?\n'
                'Reply with an amount (e.g. *1000*), or *menu* to go back.'
            )

        if digit == '3':
            # Cash: notify staff, keep bot active — do NOT escalate like option 5.
            self._set_awaiting('menu')
            agent_tools.notify_finance_staff(
                school_id,
                str(self.session.pk),
                f'Parent will bring cash to the office for {self.student.full_name} '
                f'({admission}).',
                title='Parent bringing cash to school',
            )
            return (
                'Thank you. Please bring the *cash* to the school office for '
                f'*{self.student.full_name}*.\n'
                'The bursar has been notified so they can receive you.\n\n'
                'This chat stays open — reply *menu* for other payment options, '
                'or *5* if you need to talk to someone.'
            )

        if digit == '4':
            self._set_awaiting('promise_date')
            return (
                'No problem. What date can you pay by?\n'
                'You can say *tomorrow*, *Friday*, or *not sure* if you do not know yet.\n'
                'Reply *menu* to see other payment options.'
            )

        if digit == '5':
            self._set_awaiting('')
            agent_tools.flag_for_human_escalation(
                school_id,
                str(self.session.pk),
                'Parent asked to talk to the school.',
            )
            return (
                'Understood — I am connecting you with the school now.\n'
                'A staff member will take over this chat and reply to you here shortly.\n\n'
                'Reply *menu* only if you want the payment options again instead.'
            )

        return self._payment_menu()

    def _try_rules_first_reply(self, message_body: str) -> str | None:
        """
        Kitabu-style: handle clear intents with turn-state awareness.
        Digits inside "how much?" are amounts, not menu choices.
        """
        from communications.agent import tools as agent_tools
        from communications.agent.understanding import parse_fee_intent
        from communications.agent.understanding.intent import extract_menu_digit
        from communications.services.whatsapp_delivery import format_kes

        awaiting = self._get_awaiting()
        school_id = str(self.school.id)
        admission = self.student.admission_number

        # Absolute menu digit path — never let 3/5 get mixed via NLP / tools.
        digit = extract_menu_digit(message_body)
        if digit is not None:
            if awaiting == 'partial_amount':
                self._set_awaiting('')
                return self._start_stk(float(digit))
            if awaiting == 'promise_date':
                # Bare 1–5 while asking for a date is not a menu pick.
                return (
                    'What date can you pay by? Try *tomorrow*, *Friday*, or say *not sure*.\n'
                    'Or reply *menu* for other options.'
                )
            # '', 'menu', or anything else at the main pay flow → menu option.
            return self._handle_menu_digit(digit)

        intent = parse_fee_intent(message_body, awaiting=awaiting)

        if intent.action == 'show_menu':
            return self._payment_menu()

        # Amount while we asked "how much?" (option 2) — including 1–5.
        if intent.action == 'amount_only' and intent.amount is not None:
            if awaiting == 'partial_amount' or 'awaiting_amount' in intent.signals:
                self._set_awaiting('')
                return self._start_stk(float(intent.amount))
            # Amount without context — treat as partial STK amount.
            if awaiting in ('', 'menu') and intent.confidence in ('high', 'medium'):
                # Only auto-STK bare amounts right after how-much inference.
                last = (
                    MessageLog.objects.filter(
                        school=self.school,
                        session=self.session,
                        sender=MessageLog.Sender.BOT,
                    )
                    .order_by('-created_at')
                    .values_list('body', flat=True)
                    .first()
                ) or ''
                if 'how much' in last.lower():
                    self._set_awaiting('')
                    return self._start_stk(float(intent.amount))

        if intent.action == 'pay_later':
            amount = intent.amount
            if amount is None:
                amount = self._outstanding_total()
            if amount is None or amount <= 0:
                self._set_awaiting('')
                return (
                    f'There is no outstanding balance for *{self.student.full_name}* '
                    'right now.'
                )

            if intent.date_uncertain:
                result = agent_tools.record_payment_promise(
                    school_id,
                    admission,
                    float(amount),
                    'not sure',
                )
                if result.get('success') is False:
                    return result.get('message') or 'I could not save that payment promise.'
                self._set_awaiting('')
                return (
                    f'Thank you. I have noted that you will pay '
                    f'*{format_kes(amount)}* for *{self.student.full_name}*, '
                    'but you are not sure of the date yet.\n\n'
                    'We will follow up gently. Reply *1* when you are ready to pay '
                    'by M-Pesa, or *menu* for options.'
                )

            if not intent.date_result or not intent.date_result.is_ok:
                self._set_awaiting('promise_date')
                return (
                    'No problem. What date can you pay by?\n'
                    'You can say *tomorrow*, *Friday*, or *not sure* if you do not know yet.\n'
                    'Reply *menu* to see other payment options.'
                )

            result = agent_tools.record_payment_promise(
                school_id,
                admission,
                float(amount),
                intent.date_result.value.isoformat(),
            )
            if result.get('success') is False:
                return result.get('message') or 'I could not save that payment promise.'

            self._set_awaiting('')
            when = intent.date_result.value.isoformat()
            return (
                f'Thank you. I have recorded a promise of *{format_kes(amount)}* '
                f'for *{self.student.full_name}* by *{when}*.\n\n'
                'We will pause fee reminders until then. '
                'Reply *1* anytime if you want to pay by M-Pesa sooner, '
                'or *menu* for options.'
            )

        if intent.action == 'pay_now_mpesa' and intent.confidence == 'high':
            amount = intent.amount
            if amount is None:
                amount = self._outstanding_total()
            if amount is None or amount <= 0:
                self._set_awaiting('')
                return (
                    f'There is no outstanding balance for *{self.student.full_name}* '
                    'right now.'
                )
            self._set_awaiting('')
            return self._start_stk(float(amount))

        if intent.action == 'pay_partial':
            if intent.amount is None:
                self._set_awaiting('partial_amount')
                return (
                    'How much would you like to pay today?\n'
                    'Reply with an amount (e.g. *1000*), or *menu* to go back.'
                )
            self._set_awaiting('')
            return self._start_stk(float(intent.amount))

        if intent.action == 'bring_cash' and intent.confidence == 'high':
            # Natural-language cash — same as menu 3 (notify only, no escalate).
            self._set_awaiting('menu')
            agent_tools.notify_finance_staff(
                school_id,
                str(self.session.pk),
                f'Parent will bring cash to the office for {self.student.full_name} '
                f'({admission}).',
                title='Parent bringing cash to school',
            )
            return (
                'Thank you. Please bring the *cash* to the school office for '
                f'*{self.student.full_name}*.\n'
                'The bursar has been notified so they can receive you.\n\n'
                'This chat stays open — reply *menu* for other payment options, '
                'or *5* if you need to talk to someone.'
            )

        if intent.action == 'talk_to_school':
            # Never escalate menu-5 while we were collecting a partial amount.
            if awaiting == 'partial_amount':
                self._set_awaiting('partial_amount')
                return (
                    'Please reply with the *amount* in KES (for example *1000*), '
                    'or *menu* to go back to payment options.'
                )
            self._set_awaiting('')
            agent_tools.flag_for_human_escalation(
                school_id,
                str(self.session.pk),
                'Parent asked to talk to the school.',
            )
            return (
                'Understood — I am connecting you with the school now.\n'
                'A staff member will take over this chat and reply to you here shortly.\n\n'
                'Reply *menu* only if you want the payment options again instead.'
            )

        # Soft re-prompt when awaiting amount/date and message was unclear.
        if awaiting == 'partial_amount':
            return (
                'Please reply with the *amount* in KES (for example *1000*), '
                'or *menu* to go back.'
            )
        if awaiting == 'promise_date':
            return (
                'What date can you pay by? Try *tomorrow*, *Friday*, or say *not sure*.\n'
                'Or reply *menu* for other options.'
            )

        return None

    def _start_stk(self, amount: float) -> str:
        from communications.agent import tools as agent_tools
        from communications.services.whatsapp_delivery import format_kes

        self._set_awaiting('')
        result = agent_tools.initiate_fee_payment(
            str(self.school.id),
            self.student.admission_number,
            self._stk_phone(),
            float(amount),
        )
        if result.get('success'):
            phone = self._stk_phone()
            tail = phone[-4:] if phone and len(phone) >= 4 else ''
            phone_note = f' on the phone ending *{tail}*' if tail else ''
            return (
                f'I have sent an M-Pesa prompt for *{format_kes(amount)}* '
                f'for *{self.student.full_name}*{phone_note}. '
                'Enter your PIN on your phone to complete.\n'
                'If you do not see it, check M-Pesa → Lipa na M-Pesa prompts, '
                'or reply *1* / the amount again to resend.\n'
                'Reply *menu* for other options.'
            )
        detail = (result.get('message') or '').strip()
        if detail and detail.lower() not in {
            'unable to initiate payment.',
            'failed to initiate m-pesa payment.',
        }:
            return (
                f'I could not start the M-Pesa payment ({detail}). '
                'Please try again in a moment, or reply *menu* / *5* to talk to the school.'
            )
        return (
            'I could not start the M-Pesa payment right now. '
            'Please try again shortly, or reply *menu* / *5* to talk to the school.'
        )

    def _build_bound_tools(self) -> list:
        """
        Bind tenant + student context into tool callables.

        The model cannot choose an arbitrary school_id; wrappers force
        the orchestrator's school / session / active student.
        """
        from communications.agent import tools as agent_tools

        school_id = str(self.school.id)
        admission = self.student.admission_number
        session_id = str(self.session.pk)
        parent_phone = self.session.parent_contact.phone_number

        def get_student_fee_balance() -> dict:
            """Look up the active student's fee balances across all unsettled terms."""
            return agent_tools.get_student_fee_balance(school_id, admission)

        stk_default = self._stk_phone()

        def initiate_fee_payment(amount: float, phone_number: str = '') -> dict:
            """
            Start an M-Pesa STK push for the active student's fees.

            Args:
                amount: Amount to collect (must be > 0).
                phone_number: Optional payer phone; defaults to the roster parent phone.
            """
            phone = (phone_number or '').strip() or stk_default or parent_phone
            return agent_tools.initiate_fee_payment(
                school_id,
                admission,
                phone,
                amount,
            )

        def record_payment_promise(
            promised_amount: float,
            promised_date: str = '',
        ) -> dict:
            """
            Record a PENDING promise to pay by a future date.

            Args:
                promised_amount: Amount the parent commits to pay (use full outstanding if unspecified).
                promised_date: Natural date ("tomorrow", "Friday"), YYYY-MM-DD, or "not sure".
            """
            return agent_tools.record_payment_promise(
                school_id,
                admission,
                promised_amount,
                promised_date,
            )

        def flag_for_human_escalation(reason: str) -> dict:
            """
            Escalate this WhatsApp conversation to school staff.

            Args:
                reason: Short reason (dispute, frustration, request for human, etc.).
            """
            return agent_tools.flag_for_human_escalation(
                school_id,
                session_id,
                reason,
            )

        def notify_finance_staff(reason: str, title: str = 'Parent WhatsApp update') -> dict:
            """
            Notify finance staff without escalating (e.g. parent bringing cash).

            Args:
                reason: What staff should know.
                title: Short notification title.
            """
            return agent_tools.notify_finance_staff(
                school_id,
                session_id,
                reason,
                title=title,
            )

        # Stable names for Gemini function calling / AFC.
        get_student_fee_balance.__name__ = 'get_student_fee_balance'
        initiate_fee_payment.__name__ = 'initiate_fee_payment'
        record_payment_promise.__name__ = 'record_payment_promise'
        flag_for_human_escalation.__name__ = 'flag_for_human_escalation'
        notify_finance_staff.__name__ = 'notify_finance_staff'

        return [
            get_student_fee_balance,
            initiate_fee_payment,
            record_payment_promise,
            flag_for_human_escalation,
            notify_finance_staff,
        ]

    def _load_history_contents(self) -> list[Any]:
        from google.genai import types

        logs = list(
            MessageLog.objects.filter(
                school=self.school,
                session=self.session,
            )
            .order_by('-created_at')[:HISTORY_LIMIT]
        )
        logs.reverse()  # chronological

        contents: list[Any] = []
        for log in logs:
            text = (log.body or '').strip()
            if not text:
                continue
            # Skip internal audit markers from model-facing history noise if desired;
            # keep them so the model knows escalation already happened.
            if log.sender == MessageLog.Sender.PARENT:
                role = 'user'
            else:
                role = 'model'
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=text)],
                )
            )
        return contents

    def handle_incoming_message(self, message_body: str) -> str:
        """
        Run one parent turn through Gemini with automatic tool calling.

        Returns the final natural-language reply for WhatsApp.
        """
        from google.genai import types

        body = (message_body or '').strip()
        if not body:
            return (
                f'Hello. How can I help you today regarding '
                f'*{self.student.full_name}*?'
            )

        # Rules-first (Kitabu-style): clear pay-later / STK / cash without LLM pedantry.
        rules_reply = self._try_rules_first_reply(body)
        if rules_reply:
            return rules_reply

        contents = self._load_history_contents()
        # Webhook already persisted this inbound turn into MessageLog before we run.
        # Append only when the orchestrator body differs (e.g. post-admission kickoff).
        last_user = ''
        if contents and getattr(contents[-1], 'role', None) == 'user':
            parts = getattr(contents[-1], 'parts', None) or []
            if parts:
                last_user = (getattr(parts[0], 'text', None) or '').strip()
        if last_user != body:
            contents.append(
                types.Content(
                    role='user',
                    parts=[types.Part.from_text(text=body)],
                )
            )
        if not contents:
            contents.append(
                types.Content(
                    role='user',
                    parts=[types.Part.from_text(text=body or 'Hello')],
                )
            )

        config = types.GenerateContentConfig(
            system_instruction=self._system_instruction,
            tools=self._tools,
            temperature=0.35,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=8,
            ),
        )

        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )
        except Exception:
            logger.exception(
                'Gemini orchestrator failed school=%s session=%s student=%s',
                self.school.id,
                self.session.pk,
                self.student.admission_number,
            )
            return (
                'I am having trouble processing that right now. '
                'A staff member can help shortly if you prefer — '
                'please reply *speak to staff*.'
            )

        text = (getattr(response, 'text', None) or '').strip()
        if text:
            return text

        # Fallback if the model returned only function calls / empty parts.
        logger.warning(
            'Gemini returned empty text school=%s session=%s',
            self.school.id,
            self.session.pk,
        )
        return (
            f'Thank you. I have noted your message about '
            f'*{self.student.full_name}*. '
            'How else can I help with fees or school administration today?'
        )
