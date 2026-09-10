"""Twilio webhook and school broadcast announcement views."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from academics.models import ClassStream, GradeLevel
from communications.agent.orchestrator import KoraAgentOrchestrator
from communications.agent.tools import flag_for_human_escalation
from communications.forms import BroadcastNoticeForm
from communications.models import BroadcastNotice, ConversationSession, MessageLog
from communications.services.broadcast import (
    enqueue_broadcast_notice,
    resolve_broadcast_phones,
)
from communications.services.identity import (
    get_or_create_active_session,
    normalize_incoming_phone,
    resolve_conversation_context,
    resolve_parent_identity,
    try_link_by_admission_number,
)
from communications.services.twilio_service import send_whatsapp_message
from tenants.decorators import school_admin_required
from tenants.models import Notification, School, SchoolMembership
from tenants.services import notify_user

logger = logging.getLogger(__name__)
User = get_user_model()

TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?>\n<Response></Response>'

HANDOFF_MESSAGE = (
    'I have escalated this issue to the school bursar. '
    'A staff member will review your details and reach out shortly.'
)
AGENT_FAILURE_MESSAGE = (
    'We are experiencing a temporary delay. '
    'A school administrator has been notified to assist you.'
)


def _twiml_response() -> HttpResponse:
    return HttpResponse(TWIML_EMPTY, content_type='text/xml; charset=utf-8')


def resolve_webhook_school(tenant_id: str | None):
    """Resolve school from ?tenant_id=… or fall back to default active school."""
    if tenant_id:
        school = School.objects.filter(pk=tenant_id, is_active=True).first()
        if school is not None:
            return school
        logger.warning('Twilio webhook unknown tenant_id=%s', tenant_id)

    default_code = (
        getattr(settings, 'TWILIO_DEFAULT_SCHOOL_CODE', '') or 'greenfields-academy'
    ).strip()
    if default_code:
        school = School.objects.filter(code=default_code, is_active=True).first()
        if school is not None:
            return school

    return School.objects.filter(is_active=True).order_by('created_at').first()


def _notify_staff_inbox(*, school, from_phone: str, body: str, session) -> None:
    """Push a dashboard notification for staff-handled conversations."""
    preview = (body or '').strip()
    if len(preview) > 240:
        preview = f'{preview[:237]}...'
    title = 'Parent WhatsApp message (staff conversation)'
    note = (
        f'From {from_phone}'
        + (f' · session {session.pk}' if session else '')
        + (f'\n{preview}' if preview else '')
    )
    admin_ids = list(
        SchoolMembership.objects.filter(
            school=school,
            is_admin=True,
            user__is_active=True,
        ).values_list('user_id', flat=True)
    )
    for user in User.objects.filter(pk__in=admin_ids):
        notify_user(
            user=user,
            school=school,
            kind=Notification.Kind.NOTICE,
            title=title,
            body=note,
        )
    logger.info(
        'Twilio staff-routed message school=%s from=%s session=%s body=%r',
        school.id,
        from_phone,
        getattr(session, 'pk', None),
        preview,
    )


def _run_bot_agent(*, school, session, student, from_phone: str, body: str) -> None:
    """Execute KoraAgentOrchestrator and send the appropriate WhatsApp reply."""
    try:
        orchestrator = KoraAgentOrchestrator(
            school=school,
            session=session,
            student=student,
        )
        reply_text = orchestrator.handle_incoming_message(body)
    except Exception:
        logger.exception(
            'Kora agent failed school=%s session=%s student=%s',
            school.id,
            session.pk,
            getattr(student, 'admission_number', None),
        )
        flag_for_human_escalation(
            str(school.id),
            str(session.pk),
            'LLM failure or timeout while handling parent WhatsApp message.',
        )
        send_whatsapp_message(
            school,
            from_phone,
            AGENT_FAILURE_MESSAGE,
            session=session,
            sender_type=MessageLog.Sender.BOT,
        )
        return

    session.refresh_from_db(fields=['status'])
    if session.status == ConversationSession.Status.ESCALATED_PENDING:
        outbound = HANDOFF_MESSAGE
    else:
        outbound = (reply_text or '').strip() or (
            f'Thank you. How else can I help regarding *{student.full_name}*?'
        )

    send_whatsapp_message(
        school,
        from_phone,
        outbound,
        session=session,
        sender_type=MessageLog.Sender.BOT,
    )


@csrf_exempt
@require_POST
def twilio_whatsapp_webhook(request):
    """
    Inbound Twilio WhatsApp webhook.

    Query: ?tenant_id=<school UUID>
    Always returns empty TwiML (outbound replies go via REST API).
    """
    tenant_id = (request.GET.get('tenant_id') or '').strip() or None
    school = resolve_webhook_school(tenant_id)
    if school is None:
        logger.error('Twilio webhook: no active school available')
        return _twiml_response()

    raw_from = (request.POST.get('From') or '').strip()
    body = request.POST.get('Body') or ''
    message_sid = (request.POST.get('MessageSid') or '').strip() or None

    try:
        from_phone = normalize_incoming_phone(raw_from)
    except ValueError:
        logger.warning('Twilio webhook invalid From=%r', raw_from)
        return _twiml_response()

    parent_contact = resolve_parent_identity(school, from_phone)
    session = get_or_create_active_session(school, parent_contact)

    MessageLog.objects.create(
        school=school,
        session=session,
        direction=MessageLog.Direction.INBOUND,
        sender=MessageLog.Sender.PARENT,
        body=body,
        twilio_message_sid=message_sid,
        delivery_status=MessageLog.DeliveryStatus.DELIVERED,
    )

    # Staff / escalated queues: no automated agent replies.
    if session.status in (
        ConversationSession.Status.STAFF_ACTIVE,
        ConversationSession.Status.ESCALATED_PENDING,
    ):
        _notify_staff_inbox(
            school=school,
            from_phone=from_phone,
            body=body,
            session=session,
        )
        return _twiml_response()

    # Unverified / unlinked: attempt admission-number registration from body.
    if not parent_contact.students.exists():
        linked = try_link_by_admission_number(school, parent_contact, body)
        if linked is None and (body or '').strip():
            send_whatsapp_message(
                school,
                from_phone,
                (
                    f'We could not find admission number “{(body or "").strip()}”. '
                    'Please check and reply with the correct Student Admission Number.'
                ),
                session=session,
                sender_type=MessageLog.Sender.BOT,
            )
            return _twiml_response()

    resolution = resolve_conversation_context(school, from_phone, body)
    session = resolution.session

    if not resolution.can_proceed:
        if resolution.reply_message:
            send_whatsapp_message(
                school,
                from_phone,
                resolution.reply_message,
                session=session,
                sender_type=MessageLog.Sender.BOT,
            )
        return _twiml_response()

    session.refresh_from_db(fields=['status', 'active_student'])
    if session.status in (
        ConversationSession.Status.STAFF_ACTIVE,
        ConversationSession.Status.ESCALATED_PENDING,
    ):
        _notify_staff_inbox(
            school=school,
            from_phone=from_phone,
            body=body,
            session=session,
        )
        return _twiml_response()

    student = resolution.active_student or session.active_student
    if session.status != ConversationSession.Status.BOT_ACTIVE or student is None:
        logger.warning(
            'Twilio webhook skipped agent status=%s student=%s session=%s',
            session.status,
            getattr(student, 'pk', None),
            session.pk,
        )
        return _twiml_response()

    _run_bot_agent(
        school=school,
        session=session,
        student=student,
        from_phone=from_phone,
        body=body,
    )
    return _twiml_response()


# ---------------------------------------------------------------------------
# School broadcast announcements (dashboard)
# ---------------------------------------------------------------------------


def _broadcast_school(request):
    school = getattr(request, 'school', None)
    if school is None:
        messages.error(request, 'No school context available for this account.')
    return school


def _broadcast_page_context(request, school, form=None):
    form = form or BroadcastNoticeForm(school=school)
    grades = list(
        GradeLevel.objects.filter(school=school).order_by('order', 'name')
    )
    streams = list(
        ClassStream.objects.filter(school=school)
        .select_related('grade_level')
        .order_by('grade_level__order', 'name')
    )
    broadcasts = list(
        BroadcastNotice.objects.filter(school=school)
        .select_related('created_by')
        .order_by('-created_at')[:50]
    )
    streams_by_grade = {}
    for stream in streams:
        streams_by_grade.setdefault(str(stream.grade_level_id), []).append(
            {'id': stream.pk, 'label': str(stream)}
        )
    return {
        'page_title': 'Broadcasts',
        'form': form,
        'grades': grades,
        'streams': streams,
        'broadcasts': broadcasts,
        'streams_by_grade_json': json.dumps(streams_by_grade),
        'audience_all': BroadcastNotice.TargetAudience.ALL_PARENTS,
        'audience_grade': BroadcastNotice.TargetAudience.GRADE_LEVEL,
        'audience_stream': BroadcastNotice.TargetAudience.CLASS_STREAM,
    }


@method_decorator(school_admin_required, name='dispatch')
class BroadcastListView(LoginRequiredMixin, View):
    """Compose form + historical broadcast delivery log."""

    template_name = 'dashboard/broadcasts.html'

    def get(self, request):
        school = _broadcast_school(request)
        if school is None:
            return redirect('tenants:select')
        return render(
            request,
            self.template_name,
            _broadcast_page_context(request, school),
        )

    def post(self, request):
        return BroadcastCreateView.as_view()(request)


@method_decorator(school_admin_required, name='dispatch')
class BroadcastCreateView(LoginRequiredMixin, View):
    """Create a broadcast notice and enqueue WhatsApp delivery."""

    template_name = 'dashboard/broadcasts.html'

    def get(self, request):
        return redirect('dashboard:broadcast_list')

    def post(self, request):
        school = _broadcast_school(request)
        if school is None:
            return redirect('tenants:select')

        form = BroadcastNoticeForm(request.POST, school=school)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                _broadcast_page_context(request, school, form=form),
            )

        phones = resolve_broadcast_phones(
            school,
            form.cleaned_data['target_audience'],
            form.cleaned_data.get('target_id'),
        )
        if not phones:
            form.add_error(
                None,
                'No unique parent phone numbers match this audience.',
            )
            return render(
                request,
                self.template_name,
                _broadcast_page_context(request, school, form=form),
            )

        notice = BroadcastNotice.objects.create(
            school=school,
            title=form.cleaned_data['title'].strip(),
            message=form.cleaned_data['message'].strip(),
            target_audience=form.cleaned_data['target_audience'],
            target_id=form.cleaned_data.get('target_id'),
            total_recipients=len(phones),
            sent_count=0,
            status=BroadcastNotice.Status.SENDING,
            created_by=request.user,
        )
        enqueue_broadcast_notice(notice.pk)
        messages.success(
            request,
            f'Broadcast "{notice.title}" queued for {len(phones)} parent'
            f'{"s" if len(phones) != 1 else ""}. Delivery runs in the background.',
        )
        return redirect('dashboard:broadcast_list')


@method_decorator(school_admin_required, name='dispatch')
class BroadcastPreviewView(LoginRequiredMixin, View):
    """HTMX/JSON preview of unique parent recipient count."""

    def get(self, request):
        school = _broadcast_school(request)
        if school is None:
            return JsonResponse({'count': 0, 'error': 'No school'}, status=403)

        audience = (
            request.GET.get('target_audience')
            or BroadcastNotice.TargetAudience.ALL_PARENTS
        ).strip().upper()
        raw_target = (request.GET.get('target_id') or '').strip()
        target_id = int(raw_target) if raw_target.isdigit() else None

        if audience in (
            BroadcastNotice.TargetAudience.GRADE_LEVEL,
            BroadcastNotice.TargetAudience.CLASS_STREAM,
        ) and not target_id:
            count = 0
        else:
            count = len(resolve_broadcast_phones(school, audience, target_id))

        if request.headers.get('HX-Request'):
            label = 'parent' if count == 1 else 'parents'
            return HttpResponse(
                f'<span id="preview-count" class="font-mono text-yellow-400">{count}</span>'
                f'<span class="text-zinc-400"> unique {label}</span>'
            )
        return JsonResponse({'count': count})
