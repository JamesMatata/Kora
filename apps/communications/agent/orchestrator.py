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
    return f"""You are Kora, an efficient, polite, and professional school administrative assistant for {school_name}.

You are speaking with a parent/guardian on WhatsApp about their child:
- Student name: {student_name}
- Admission number: {admission}

## Core behavioural guidelines
1. Always address the parent respectfully and professionally.
2. Base every financial answer strictly on tool outputs. Never fabricate balances, deadlines, discounts, receipts, or payment confirmations.
3. If the parent wants to pay now, confirm the amount, then call `initiate_fee_payment` with their phone number and the agreed amount.
4. If the parent cannot pay now but offers a future date, call `record_payment_promise` with the promised amount and date (YYYY-MM-DD).
5. If the parent disputes an amount (e.g. claims they already paid cash/bank and it is not reflected) or shows severe frustration / anger, immediately call `flag_for_human_escalation` with a clear reason — do not argue.
6. Keep replies concise and WhatsApp-friendly: short paragraphs, clear amounts, and *bold* for key terms (WhatsApp uses single asterisks).
7. Prefer calling `get_student_fee_balance` before discussing money so figures stay accurate.
8. Do not invent school policies. If unsure or the request is outside fees/admin help, escalate with `flag_for_human_escalation`.

## Tool use
- You have no direct database or payment access. Use only the provided tools.
- Tools are already scoped to this school and the active student where applicable.
- After a tool returns, summarise the result clearly for the parent.
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
            """Look up the active student's current-term fee balance."""
            return agent_tools.get_student_fee_balance(school_id, admission)

        def initiate_fee_payment(amount: float, phone_number: str = '') -> dict:
            """
            Start an M-Pesa STK push for the active student's fees.

            Args:
                amount: Amount to collect (must be > 0).
                phone_number: Optional payer phone; defaults to the parent's WhatsApp number.
            """
            phone = (phone_number or '').strip() or parent_phone
            return agent_tools.initiate_fee_payment(
                school_id,
                admission,
                phone,
                amount,
            )

        def record_payment_promise(
            promised_amount: float,
            promised_date: str,
        ) -> dict:
            """
            Record a PENDING promise to pay by a future date.

            Args:
                promised_amount: Amount the parent commits to pay.
                promised_date: Date as YYYY-MM-DD.
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

        # Stable names for Gemini function calling / AFC.
        get_student_fee_balance.__name__ = 'get_student_fee_balance'
        initiate_fee_payment.__name__ = 'initiate_fee_payment'
        record_payment_promise.__name__ = 'record_payment_promise'
        flag_for_human_escalation.__name__ = 'flag_for_human_escalation'

        return [
            get_student_fee_balance,
            initiate_fee_payment,
            record_payment_promise,
            flag_for_human_escalation,
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

        contents = self._load_history_contents()
        contents.append(
            types.Content(
                role='user',
                parts=[types.Part.from_text(text=body)],
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
