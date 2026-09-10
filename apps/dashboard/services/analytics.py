"""Weekly executive briefing aggregation and Gemini summarization."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from communications.models import ConversationSession, MessageLog
from dashboard.models import ExecutiveWeeklyReport
from finance.models import FeeInvoice, PaymentTransaction

logger = logging.getLogger(__name__)

MODEL_NAME = 'gemini-2.5-flash'
DISPUTE_KEYWORDS = (
    'dispute',
    'wrong',
    'error',
    'incorrect',
    'already paid',
    'complaint',
    'unfair',
    'overcharge',
    'receipt',
    'refund',
    'not received',
    'balance',
    'promise',
    'cannot pay',
    'can\'t pay',
    'hardship',
)


@dataclass
class WeeklyMetrics:
    school_name: str
    period_start: str
    period_end: str
    week_number: int
    year: int
    total_billed: Decimal
    total_collected: Decimal
    outstanding: Decimal
    collection_efficiency: Decimal
    successful_stk_pushes: int
    total_conversations: int
    automated_resolved_count: int
    escalated_count: int
    escalations_by_reason: dict[str, int] = field(default_factory=dict)
    parent_concern_excerpts: list[str] = field(default_factory=list)

    def to_prompt_payload(self) -> dict:
        payload = asdict(self)
        for key in (
            'total_billed',
            'total_collected',
            'outstanding',
            'collection_efficiency',
        ):
            payload[key] = float(payload[key])
        return payload


def _money(value) -> Decimal:
    if value is None:
        return Decimal('0.00')
    return Decimal(value).quantize(Decimal('0.01'))


def _anonymize_excerpt(body: str, limit: int = 180) -> str:
    text = (body or '').strip()
    text = re.sub(r'\+?\d[\d\s\-()]{7,}\d', '[phone]', text)
    text = re.sub(
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
        '[email]',
        text,
    )
    # Soft-redact likely personal names after common greetings.
    text = re.sub(
        r'\b(my (son|daughter|child)\s+)[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?',
        r'\1[student]',
        text,
        flags=re.IGNORECASE,
    )
    text = ' '.join(text.split())
    if len(text) > limit:
        text = f'{text[: limit - 1]}…'
    return text


def _normalize_escalation_reason(body: str) -> str:
    reason = (body or '').replace('[ESCALATION]', '', 1).strip()
    reason = ' '.join(reason.split())
    if not reason:
        return 'Unspecified'
    if len(reason) > 80:
        reason = f'{reason[:77]}…'
    return reason


def collect_weekly_metrics(school, *, period_end: datetime | None = None) -> WeeklyMetrics:
    """Aggregate trailing-7-day finance and conversation metrics for a school."""
    period_end = period_end or timezone.now()
    period_start = period_end - timedelta(days=7)
    iso = period_end.isocalendar()
    week_number = int(iso.week)
    year = int(iso.year)

    total_billed = _money(
        FeeInvoice.objects.filter(
            school=school,
            created_at__gte=period_start,
            created_at__lte=period_end,
        ).aggregate(total=Coalesce(Sum('total_amount'), Decimal('0.00')))['total']
    )

    total_collected = _money(
        PaymentTransaction.objects.filter(
            school=school,
            status=PaymentTransaction.Status.SUCCESS,
            created_at__gte=period_start,
            created_at__lte=period_end,
        ).aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total']
    )

    outstanding = total_billed - total_collected
    if outstanding < 0:
        outstanding = Decimal('0.00')

    if total_billed > 0:
        efficiency = (total_collected / total_billed * Decimal('100')).quantize(
            Decimal('0.01')
        )
    else:
        efficiency = Decimal('0.00')

    successful_stk = PaymentTransaction.objects.filter(
        school=school,
        status=PaymentTransaction.Status.SUCCESS,
        created_at__gte=period_start,
        created_at__lte=period_end,
    ).count()

    conversations = ConversationSession.objects.filter(
        school=school,
    ).filter(
        Q(created_at__gte=period_start, created_at__lte=period_end)
        | Q(last_message_at__gte=period_start, last_message_at__lte=period_end)
    )
    total_conversations = conversations.count()

    escalated_session_ids = set(
        MessageLog.objects.filter(
            school=school,
            body__startswith='[ESCALATION]',
            created_at__gte=period_start,
            created_at__lte=period_end,
        ).values_list('session_id', flat=True)
    )
    escalated_count = len(escalated_session_ids)

    automated_resolved_count = (
        conversations.filter(status=ConversationSession.Status.CLOSED)
        .exclude(pk__in=escalated_session_ids)
        .count()
    )

    reason_counter: Counter[str] = Counter()
    for body in MessageLog.objects.filter(
        school=school,
        body__startswith='[ESCALATION]',
        created_at__gte=period_start,
        created_at__lte=period_end,
    ).values_list('body', flat=True):
        reason_counter[_normalize_escalation_reason(body)] += 1

    keyword_q = Q()
    for word in DISPUTE_KEYWORDS:
        keyword_q |= Q(body__icontains=word)

    concern_logs = (
        MessageLog.objects.filter(
            school=school,
            sender=MessageLog.Sender.PARENT,
            direction=MessageLog.Direction.INBOUND,
            created_at__gte=period_start,
            created_at__lte=period_end,
        )
        .filter(keyword_q)
        .order_by('-created_at')[:12]
    )
    excerpts = []
    seen = set()
    for log in concern_logs:
        excerpt = _anonymize_excerpt(log.body)
        if not excerpt or excerpt in seen:
            continue
        seen.add(excerpt)
        excerpts.append(excerpt)
        if len(excerpts) >= 8:
            break

    # Fallback: anonymized escalation reasons if no keyword matches.
    if not excerpts:
        for body in MessageLog.objects.filter(
            school=school,
            body__startswith='[ESCALATION]',
            created_at__gte=period_start,
            created_at__lte=period_end,
        ).order_by('-created_at')[:8].values_list('body', flat=True):
            excerpts.append(_anonymize_excerpt(_normalize_escalation_reason(body)))

    return WeeklyMetrics(
        school_name=school.name,
        period_start=timezone.localtime(period_start).isoformat(),
        period_end=timezone.localtime(period_end).isoformat(),
        week_number=week_number,
        year=year,
        total_billed=total_billed,
        total_collected=total_collected,
        outstanding=outstanding,
        collection_efficiency=efficiency,
        successful_stk_pushes=successful_stk,
        total_conversations=total_conversations,
        automated_resolved_count=automated_resolved_count,
        escalated_count=escalated_count,
        escalations_by_reason=dict(reason_counter.most_common(12)),
        parent_concern_excerpts=excerpts,
    )


def _extract_json_object(text: str) -> dict:
    raw = (text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
        raw = re.sub(r'\s*```$', '', raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', raw, flags=re.DOTALL)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    raise ValueError('Gemini response did not contain a JSON object.')


def _fallback_summary(metrics: WeeklyMetrics) -> tuple[str, list[str]]:
    reasons = metrics.escalations_by_reason or {'Unspecified': metrics.escalated_count}
    top_reasons = ', '.join(
        f'{reason} ({count})' for reason, count in list(reasons.items())[:3]
    ) or 'none recorded'
    summary = (
        f'## Weekly executive briefing — {metrics.school_name}\n\n'
        f'**Period:** {metrics.period_start} → {metrics.period_end}\n\n'
        f'### Financial performance\n'
        f'- Billed: KSh {metrics.total_billed:,.2f}\n'
        f'- Collected: KSh {metrics.total_collected:,.2f}\n'
        f'- Outstanding (window): KSh {metrics.outstanding:,.2f}\n'
        f'- Collection efficiency: {metrics.collection_efficiency}%\n'
        f'- Successful STK pushes: {metrics.successful_stk_pushes}\n\n'
        f'### Parent conversations\n'
        f'- Conversations: {metrics.total_conversations}\n'
        f'- Automated resolutions: {metrics.automated_resolved_count}\n'
        f'- Escalations: {metrics.escalated_count}\n'
        f'- Top escalation reasons: {top_reasons}\n\n'
        f'_Generated without Gemini (API unavailable)._\n'
    )
    actions = [
        'Follow up on overdue balances with the highest outstanding invoices first.',
        'Review escalated WhatsApp threads and close loops with parents this week.',
        'Confirm Paybill / STK credentials and monitor failed payment attempts daily.',
    ]
    return summary, actions


def generate_executive_narrative(metrics: WeeklyMetrics) -> tuple[str, list[str]]:
    """Call Gemini to produce summary markdown + 3 action items."""
    api_key = (getattr(settings, 'GEMINI_API_KEY', '') or '').strip()
    model_name = (
        getattr(settings, 'GEMINI_MODEL', MODEL_NAME) or MODEL_NAME
    ).strip()
    if not api_key:
        logger.warning('GEMINI_API_KEY missing; using fallback weekly summary.')
        return _fallback_summary(metrics)

    system_instruction = (
        "You are Kora's Chief Revenue Analyst writing for a school principal. "
        'Produce a crisp executive briefing from the provided weekly statistics. '
        'Highlight financial performance, collection efficiency, common parent '
        'concerns, and exactly 3 actionable recommendations for the bursar. '
        'Do not invent figures; only use the supplied metrics. '
        'Keep parent excerpts anonymized. '
        'Respond with JSON only using keys summary_markdown (GitHub-flavored '
        'Markdown string) and key_action_items (array of exactly 3 short strings).'
    )
    user_prompt = (
        'Weekly school metrics and anonymized parent feedback:\n'
        f'{json.dumps(metrics.to_prompt_payload(), indent=2)}\n\n'
        'Return JSON only.'
    )

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model_name,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.35,
                response_mime_type='application/json',
            ),
        )
        text = (getattr(response, 'text', None) or '').strip()
        data = _extract_json_object(text)
        summary = (data.get('summary_markdown') or '').strip()
        actions = data.get('key_action_items') or []
        if not isinstance(actions, list):
            actions = []
        actions = [str(item).strip() for item in actions if str(item).strip()][:3]
        while len(actions) < 3:
            actions.append('Review outstanding fee balances with class teachers.')
        if not summary:
            return _fallback_summary(metrics)
        return summary, actions
    except Exception:
        logger.exception('Gemini weekly briefing failed for %s', metrics.school_name)
        return _fallback_summary(metrics)


def generate_weekly_report(
    school,
    *,
    period_end: datetime | None = None,
    force: bool = False,
) -> tuple[ExecutiveWeeklyReport, str]:
    """
    Aggregate trailing-week metrics, ask Gemini for a briefing, and persist.

    Returns (report, status) where status is 'created', 'updated', or 'exists'.
    """
    metrics = collect_weekly_metrics(school, period_end=period_end)
    existing = ExecutiveWeeklyReport.objects.filter(
        school=school,
        year=metrics.year,
        week_number=metrics.week_number,
    ).first()
    if existing is not None and not force:
        return existing, 'exists'

    summary, actions = generate_executive_narrative(metrics)
    defaults = {
        'total_billed': metrics.total_billed,
        'total_collected': metrics.total_collected,
        'collection_efficiency': metrics.collection_efficiency,
        'total_conversations': metrics.total_conversations,
        'automated_resolved_count': metrics.automated_resolved_count,
        'escalated_count': metrics.escalated_count,
        'summary_markdown': summary,
        'key_action_items': actions,
    }
    report, created = ExecutiveWeeklyReport.objects.update_or_create(
        school=school,
        year=metrics.year,
        week_number=metrics.week_number,
        defaults=defaults,
    )
    return report, ('created' if created else 'updated')
