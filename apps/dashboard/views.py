import csv
import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView

from academics.models import AcademicYear, ClassStream, GradeLevel, Student
from communications.models import ConversationSession, MessageLog
from communications.services.twilio_service import send_whatsapp_message
from dashboard.forms import (
    ClassTeacherAssignForm,
    GradeLevelForm,
    GradeTeacherAssignForm,
    ManualPaymentForm,
    StaffInviteForm,
    StaffMemberEditForm,
    StreamCreateForm,
    StudentForm,
    StudentImportUploadForm,
)
from dashboard.models import ExecutiveWeeklyReport
from dashboard.services.student_import import (
    TEMPLATE_HEADERS,
    import_students_for_stream,
    parse_student_spreadsheet,
)
from finance.forms import FeeChargeForm
from finance.models import FeeInvoice, Payment, PaymentPromise, PaymentTransaction, StudentFee
from finance.services import create_and_assign_charge, stream_finance_rows
from tenants.decorators import school_admin_required
from tenants.models import SchoolMembership, StaffInvitation

User = get_user_model()


def _active_admin_count(school):
    return SchoolMembership.objects.filter(
        school=school,
        is_admin=True,
        user__is_active=True,
    ).count()


def _admin_slots_remaining(school):
    return max(0, 2 - _active_admin_count(school))


def _ensure_school(request):
    school = getattr(request, 'school', None)
    if school is None:
        messages.error(request, 'No school context available for this account.')
        return None
    return school


def _membership_role_label(membership):
    if membership.is_admin and membership.is_teacher:
        return 'Dual'
    if membership.is_admin:
        return 'Admin'
    if membership.is_teacher:
        return 'Teacher'
    return 'Staff'


def _user_can_access_stream(request, stream):
    """Effective role mode gates stream access (admin any / teacher assigned)."""
    user = request.user
    if not user.is_authenticated:
        return False
    school = getattr(request, 'school', None)
    if school is None or school.pk != stream.school_id:
        return False
    if getattr(request, 'acting_as_admin', False):
        return True
    if (
        getattr(request, 'acting_as_teacher', False)
        and stream.class_teacher_id == user.pk
    ):
        return True
    return False


def _managed_stream_queryset(request, school):
    qs = ClassStream.objects.filter(school=school).select_related('grade_level')
    if getattr(request, 'acting_as_admin', False):
        return qs
    if getattr(request, 'acting_as_teacher', False):
        return qs.filter(class_teacher=request.user)
    return qs.none()


def _get_accessible_stream(request, school, stream_slug):
    stream = get_object_or_404(
        ClassStream.objects.select_related('grade_level', 'class_teacher'),
        slug=stream_slug,
        school=school,
    )
    if not _user_can_access_stream(request, stream):
        return None
    return stream


class DashboardOverviewView(LoginRequiredMixin, TemplateView):
    """School command center — KPIs, recent M-Pesa activity, and escalations."""

    template_name = 'dashboard/index.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        school = getattr(request, 'school', None)
        acting_as_teacher = bool(getattr(request, 'acting_as_teacher', False))
        context['overview_mode'] = 'teacher' if acting_as_teacher else 'admin'
        context['page_title'] = 'My Classes' if acting_as_teacher else 'Command Center'
        if school is None:
            return context

        if acting_as_teacher:
            my_streams = list(
                ClassStream.objects.filter(
                    school=school,
                    class_teacher=request.user,
                )
                .select_related('grade_level')
                .annotate(
                    active_student_count=Count(
                        'students',
                        filter=Q(students__is_active=True),
                    )
                )
                .order_by('grade_level__order', 'name')
            )
            stream_ids = [s.pk for s in my_streams]
            my_students = Student.objects.filter(
                school=school,
                current_stream_id__in=stream_ids,
            )
            stream_cards = [
                {'stream': stream, 'student_count': stream.active_student_count}
                for stream in my_streams
            ]
            context.update(
                {
                    'my_stream_count': len(my_streams),
                    'my_student_count': my_students.filter(is_active=True).count(),
                    'inactive_student_count': my_students.filter(
                        is_active=False
                    ).count(),
                    'stream_cards': stream_cards,
                }
            )
            return context

        staff = SchoolMembership.objects.filter(school=school)
        students = Student.objects.filter(school=school, is_active=True)
        streams = ClassStream.objects.filter(school=school)

        invoice_qs = FeeInvoice.objects.filter(school=school)
        year = AcademicYear.objects.filter(school=school, is_current=True).first()
        if year is not None:
            term_invoices = invoice_qs.filter(term__icontains=year.name)
            if term_invoices.exists():
                invoice_qs = term_invoices

        total_billed = invoice_qs.aggregate(
            total=Coalesce(Sum('total_amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')

        total_collected = (
            PaymentTransaction.objects.filter(
                school=school,
                status=PaymentTransaction.Status.SUCCESS,
            ).aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total']
            or Decimal('0.00')
        )

        outstanding_balance = total_billed - total_collected
        if outstanding_balance < 0:
            outstanding_balance = Decimal('0.00')

        if total_billed > 0:
            collection_rate = round(
                (total_collected / total_billed) * Decimal('100'),
                1,
            )
        else:
            collection_rate = Decimal('0.0')

        active_escalations_count = ConversationSession.objects.filter(
            school=school,
            status=ConversationSession.Status.ESCALATED_PENDING,
        ).count()
        active_promises_count = PaymentPromise.objects.filter(
            school=school,
            status=PaymentPromise.Status.PENDING,
        ).count()

        recent_transactions = list(
            PaymentTransaction.objects.filter(school=school)
            .select_related('invoice__student')
            .order_by('-created_at')[:10]
        )

        escalation_sessions = list(
            ConversationSession.objects.filter(
                school=school,
                status=ConversationSession.Status.ESCALATED_PENDING,
            )
            .select_related('parent_contact', 'active_student', 'assigned_staff')
            .order_by('-last_message_at')[:15]
        )
        urgent_attention = []
        for esc in escalation_sessions:
            reason_log = (
                MessageLog.objects.filter(
                    school=school,
                    session=esc,
                    body__startswith='[ESCALATION]',
                )
                .order_by('-created_at')
                .first()
            )
            reason = ''
            flagged_at = esc.last_message_at
            if reason_log is not None:
                reason = reason_log.body.replace('[ESCALATION]', '', 1).strip()
                flagged_at = reason_log.created_at
            urgent_attention.append(
                {
                    'session': esc,
                    'parent_phone': esc.parent_contact.phone_number,
                    'parent_name': esc.parent_contact.parent_name,
                    'student': esc.active_student,
                    'reason': reason or 'Flagged for human review',
                    'flagged_at': flagged_at,
                }
            )

        context.update(
            {
                'staff_count': staff.count(),
                'student_count': students.count(),
                'stream_count': streams.count(),
                'admin_count': _active_admin_count(school),
                'total_billed': total_billed,
                'total_collected': total_collected,
                'outstanding_balance': outstanding_balance,
                'collection_rate': collection_rate,
                'active_escalations_count': active_escalations_count,
                'active_promises_count': active_promises_count,
                'recent_transactions': recent_transactions,
                'urgent_attention': urgent_attention,
                'current_academic_year': year,
            }
        )
        return context


# Backwards-compatible alias used across redirects / docs.
OverviewView = DashboardOverviewView


def _fee_ledger_filters(request):
    return {
        'q': (request.GET.get('q') or '').strip(),
        'grade': (request.GET.get('grade') or '').strip(),
        'stream': (request.GET.get('stream') or '').strip(),
        'status': (request.GET.get('status') or 'ALL').strip().upper() or 'ALL',
    }


def _fee_ledger_queryset(school, *, q='', grade='', stream='', status='ALL'):
    qs = (
        FeeInvoice.objects.filter(school=school)
        .select_related(
            'student',
            'student__grade_level',
            'student__current_stream',
        )
        .order_by('student__admission_number', '-due_date', '-created_at')
    )
    if grade:
        qs = qs.filter(student__grade_level_id=grade)
    if stream:
        qs = qs.filter(student__current_stream_id=stream)
    if status and status != 'ALL':
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(
            Q(student__first_name__icontains=q)
            | Q(student__last_name__icontains=q)
            | Q(student__admission_number__icontains=q)
        )
    return qs


def _conversation_for_student(school, student):
    return (
        ConversationSession.objects.filter(
            school=school,
            active_student=student,
        )
        .exclude(status=ConversationSession.Status.CLOSED)
        .order_by('-last_message_at')
        .first()
        or ConversationSession.objects.filter(
            school=school,
            parent_contact__students=student,
        )
        .exclude(status=ConversationSession.Status.CLOSED)
        .order_by('-last_message_at')
        .first()
    )


def _ledger_row_context(school, invoices):
    rows = []
    for invoice in invoices:
        student = invoice.student
        conversation = _conversation_for_student(school, student)
        profile_url = ''
        if student.current_stream_id:
            profile_url = reverse(
                'dashboard:student_edit',
                kwargs={
                    'stream_slug': student.current_stream.slug,
                    'admission_number': student.admission_number,
                },
            )
        rows.append(
            {
                'invoice': invoice,
                'student': student,
                'balance': invoice.balance,
                'conversation': conversation,
                'profile_url': profile_url,
            }
        )
    return rows


@method_decorator(school_admin_required, name='dispatch')
class FeeLedgerView(LoginRequiredMixin, View):
    """Interactive school-wide student fee ledger with HTMX search."""

    template_name = 'dashboard/fee_ledger.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        filters = _fee_ledger_filters(request)
        invoices = list(
            _fee_ledger_queryset(school, **filters)[:200]
        )
        grades = GradeLevel.objects.filter(school=school).order_by('order', 'name')
        streams = (
            ClassStream.objects.filter(school=school)
            .select_related('grade_level')
            .order_by('grade_level__order', 'name')
        )
        streams_by_grade = {}
        for stream in streams:
            streams_by_grade.setdefault(str(stream.grade_level_id), []).append(
                {'id': str(stream.pk), 'name': str(stream)}
            )

        return render(
            request,
            self.template_name,
            {
                'page_title': 'Fee ledger',
                'filters': filters,
                'status_choices': [
                    ('ALL', 'All statuses'),
                    *FeeInvoice.Status.choices,
                ],
                'grades': grades,
                'streams': streams,
                'streams_by_grade_json': json.dumps(streams_by_grade),
                'ledger_rows': _ledger_row_context(school, invoices),
                'query': filters['q'],
            },
        )


@method_decorator(school_admin_required, name='dispatch')
class FeeLedgerSearchView(LoginRequiredMixin, View):
    """HTMX partial: fee ledger table body rows."""

    template_name = 'dashboard/partials/fee_ledger_rows.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return HttpResponseForbidden('No school context.')

        filters = _fee_ledger_filters(request)
        invoices = list(_fee_ledger_queryset(school, **filters)[:200])
        return render(
            request,
            self.template_name,
            {
                'ledger_rows': _ledger_row_context(school, invoices),
                'query': filters['q'],
            },
        )


@method_decorator(school_admin_required, name='dispatch')
class FeeLedgerStkPushView(LoginRequiredMixin, View):
    """Quick STK push from the fee ledger modal."""

    def post(self, request):
        from finance.services.daraja import DarajaError, initiate_stk_push

        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        invoice_id = (request.POST.get('invoice_id') or '').strip()
        phone = (request.POST.get('phone_number') or '').strip()
        amount_raw = (request.POST.get('amount') or '').strip()

        invoice = (
            FeeInvoice.objects.filter(school=school, pk=invoice_id)
            .select_related('student')
            .first()
        )
        if invoice is None:
            messages.error(request, 'Invoice not found.')
            return redirect('dashboard:fee_ledger')

        try:
            amount = Decimal(amount_raw)
        except Exception:
            messages.error(request, 'Enter a valid amount.')
            return redirect('dashboard:fee_ledger')

        if amount <= 0:
            messages.error(request, 'Amount must be greater than zero.')
            return redirect('dashboard:fee_ledger')

        phone = phone or invoice.student.parent_phone
        try:
            ok, payment_tx, response_json = initiate_stk_push(
                school,
                invoice,
                phone,
                amount,
            )
        except (DarajaError, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect('dashboard:fee_ledger')

        if ok:
            messages.success(
                request,
                response_json.get('CustomerMessage')
                or f'STK push sent for {invoice.student.full_name} ({amount}).',
            )
        else:
            messages.error(
                request,
                payment_tx.result_desc or 'STK push failed.',
            )
        return redirect('dashboard:fee_ledger')


def _chat_session_queryset(school):
    return ConversationSession.objects.filter(school=school).select_related(
        'parent_contact',
        'active_student',
        'active_student__current_stream',
        'assigned_staff',
    )


def _chat_tab_filter(qs, tab):
    tab = (tab or 'escalated').strip().lower()
    if tab == 'bot':
        return qs.filter(status=ConversationSession.Status.BOT_ACTIVE), 'bot'
    if tab == 'all':
        return qs.exclude(status=ConversationSession.Status.CLOSED), 'all'
    return (
        qs.filter(status=ConversationSession.Status.ESCALATED_PENDING),
        'escalated',
    )


def _student_current_balance(school, student):
    if student is None:
        return None
    qs = FeeInvoice.objects.filter(school=school, student=student)
    year = AcademicYear.objects.filter(school=school, is_current=True).first()
    invoice = None
    if year is not None:
        year_qs = qs.filter(term__icontains=year.name)
        invoice = (
            year_qs.exclude(status=FeeInvoice.Status.PAID)
            .order_by('-due_date', '-created_at')
            .first()
            or year_qs.order_by('-due_date', '-created_at').first()
        )
    if invoice is None:
        invoice = (
            qs.exclude(status=FeeInvoice.Status.PAID)
            .order_by('-due_date', '-created_at')
            .first()
            or qs.order_by('-due_date', '-created_at').first()
        )
    if invoice is None:
        return Decimal('0.00')
    return invoice.balance


def _get_chat_session(school, session_id):
    return get_object_or_404(
        _chat_session_queryset(school),
        pk=session_id,
        school=school,
    )


def _chat_thread_rows(school, sessions):
    rows = []
    for session in sessions:
        last_msg = (
            MessageLog.objects.filter(school=school, session=session)
            .order_by('-created_at')
            .first()
        )
        student = session.active_student
        excerpt = ''
        if last_msg is not None:
            excerpt = (last_msg.body or '').strip()
            if excerpt.startswith('[ESCALATION]'):
                excerpt = excerpt.replace('[ESCALATION]', '', 1).strip()
            if len(excerpt) > 90:
                excerpt = f'{excerpt[:87]}…'
        rows.append(
            {
                'session': session,
                'student_name': (
                    student.full_name
                    if student is not None
                    else (
                        session.parent_contact.parent_name
                        or session.parent_contact.phone_number
                    )
                ),
                'phone': session.parent_contact.phone_number,
                'excerpt': excerpt or 'No messages yet',
                'timestamp': session.last_message_at or session.created_at,
            }
        )
    return rows


def _chat_console_context(request, school, *, tab='escalated', session=None):
    base_qs = _chat_session_queryset(school).order_by(
        '-last_message_at',
        '-created_at',
    )
    filtered_qs, active_tab = _chat_tab_filter(base_qs, tab)
    sessions = list(filtered_qs[:80])
    thread_rows = _chat_thread_rows(school, sessions)

    chat_messages = []
    current_balance = None
    if session is not None:
        chat_messages = list(
            MessageLog.objects.filter(school=school, session=session).order_by(
                'created_at'
            )
        )
        current_balance = _student_current_balance(
            school,
            session.active_student,
        )

    return {
        'page_title': 'WhatsApp console',
        'active_tab': active_tab,
        'thread_rows': thread_rows,
        'session': session,
        'chat_messages': chat_messages,
        'current_balance': current_balance,
        'status_bot_active': ConversationSession.Status.BOT_ACTIVE,
        'status_escalated': ConversationSession.Status.ESCALATED_PENDING,
        'status_staff_active': ConversationSession.Status.STAFF_ACTIVE,
    }


@method_decorator(school_admin_required, name='dispatch')
class ChatConsoleView(LoginRequiredMixin, View):
    """Two-pane WhatsApp conversation console with staff takeover."""

    template_name = 'dashboard/chat_console.html'

    def get(self, request, session_id=None):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        tab = (request.GET.get('tab') or 'escalated').strip().lower()
        session = None
        if session_id is not None:
            session = _get_chat_session(school, session_id)

        return render(
            request,
            self.template_name,
            _chat_console_context(
                request,
                school,
                tab=tab,
                session=session,
            ),
        )


@method_decorator(school_admin_required, name='dispatch')
class ChatMessagesPartialView(LoginRequiredMixin, View):
    """HTMX poll partial for the active chat message feed."""

    template_name = 'dashboard/partials/chat_message_feed.html'

    def get(self, request, session_id):
        school = _ensure_school(request)
        if school is None:
            return HttpResponseForbidden('No school context.')

        session = _get_chat_session(school, session_id)
        chat_messages = MessageLog.objects.filter(
            school=school,
            session=session,
        ).order_by('created_at')
        return render(
            request,
            self.template_name,
            {
                'session': session,
                'chat_messages': chat_messages,
            },
        )


@method_decorator(school_admin_required, name='dispatch')
class ChatSendView(LoginRequiredMixin, View):
    """Staff outbound WhatsApp reply via Twilio."""

    template_name = 'dashboard/partials/chat_message_feed.html'

    def post(self, request, session_id):
        school = _ensure_school(request)
        if school is None:
            return HttpResponseForbidden('No school context.')

        session = _get_chat_session(school, session_id)
        if session.status != ConversationSession.Status.STAFF_ACTIVE:
            return HttpResponseForbidden(
                'Claim this thread before sending staff replies.'
            )

        body = (request.POST.get('body') or '').strip()
        if body:
            ok, detail = send_whatsapp_message(
                school,
                session.parent_contact.phone_number,
                body,
                session=session,
                sender_type=MessageLog.Sender.STAFF,
            )
            if not ok:
                return HttpResponse(
                    f'<div class="px-4 py-2 text-sm text-red-400">'
                    f'Failed to send: {detail}</div>',
                    status=502,
                )

        chat_messages = MessageLog.objects.filter(
            school=school,
            session=session,
        ).order_by('created_at')
        return render(
            request,
            self.template_name,
            {
                'session': session,
                'chat_messages': chat_messages,
            },
        )


@method_decorator(school_admin_required, name='dispatch')
class ChatClaimView(LoginRequiredMixin, View):
    """Take over an escalated thread (locks AI out)."""

    def post(self, request, session_id):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        session = _get_chat_session(school, session_id)
        session.status = ConversationSession.Status.STAFF_ACTIVE
        session.assigned_staff = request.user
        session.save(update_fields=['status', 'assigned_staff'])
        messages.success(request, 'You claimed this conversation.')
        tab = (request.POST.get('tab') or request.GET.get('tab') or 'escalated')
        return redirect(
            f"{reverse('dashboard:chat_console_session', kwargs={'session_id': session.pk})}"
            f'?tab={tab}'
        )


@method_decorator(school_admin_required, name='dispatch')
class ChatReleaseView(LoginRequiredMixin, View):
    """Return a staff-held thread to the AI agent."""

    def post(self, request, session_id):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        session = _get_chat_session(school, session_id)
        session.status = ConversationSession.Status.BOT_ACTIVE
        session.assigned_staff = None
        session.save(update_fields=['status', 'assigned_staff'])
        messages.success(request, 'Conversation released back to the AI agent.')
        tab = (request.POST.get('tab') or request.GET.get('tab') or 'bot')
        return redirect(
            f"{reverse('dashboard:chat_console_session', kwargs={'session_id': session.pk})}"
            f'?tab={tab}'
        )


@method_decorator(school_admin_required, name='dispatch')
class ChatResolveView(LoginRequiredMixin, View):
    """Mark a staff-held thread as resolved (closed)."""

    def post(self, request, session_id):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        session = _get_chat_session(school, session_id)
        session.status = ConversationSession.Status.CLOSED
        session.assigned_staff = None
        session.save(update_fields=['status', 'assigned_staff'])
        messages.success(request, 'Conversation marked as resolved.')
        tab = (request.POST.get('tab') or request.GET.get('tab') or 'all')
        return redirect(f"{reverse('dashboard:chat_console')}?tab={tab}")


# Backwards-compatible alias for older fee-ledger / docs links.
ConversationConsoleView = ChatConsoleView


@method_decorator(school_admin_required, name='dispatch')
class ExecutiveReportListView(LoginRequiredMixin, View):
    """Latest weekly briefing cards + archive list."""

    template_name = 'dashboard/reports.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        reports = list(
            ExecutiveWeeklyReport.objects.filter(school=school).order_by(
                '-year',
                '-week_number',
                '-created_at',
            )[:52]
        )
        latest = reports[0] if reports else None
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Executive reports',
                'latest_report': latest,
                'reports': reports,
            },
        )


@method_decorator(school_admin_required, name='dispatch')
class ExecutiveReportDetailView(LoginRequiredMixin, View):
    """Full AI markdown briefing for one weekly report."""

    template_name = 'dashboard/report_detail.html'

    def get(self, request, report_id):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        report = get_object_or_404(
            ExecutiveWeeklyReport,
            pk=report_id,
            school=school,
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': f'Week {report.week_number} · {report.year}',
                'report': report,
            },
        )


@method_decorator(school_admin_required, name='dispatch')
class TeacherManagementView(LoginRequiredMixin, View):
    template_name = 'dashboard/teachers.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        return render(request, self.template_name, self._context(request, school))

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        action = (request.POST.get('action') or 'invite').strip()
        slots = _admin_slots_remaining(school)

        if action == 'edit_staff':
            membership = get_object_or_404(
                SchoolMembership.objects.select_related('user'),
                pk=request.POST.get('membership_id'),
                school=school,
            )
            # If they already count as an active admin, treat slot as available for them.
            effective_slots = slots
            if membership.is_admin and membership.user.is_active:
                effective_slots = max(slots, 1)
            form = StaffMemberEditForm(
                request.POST,
                school=school,
                membership=membership,
                admin_slots_remaining=effective_slots,
            )
            if form.is_valid():
                updated = form.save()
                messages.success(
                    request,
                    f'Updated {updated.user.get_full_name() or updated.user.email}.',
                )
                return redirect('dashboard:teachers')
            messages.error(request, 'Could not update staff member. Check the form.')
            return render(
                request,
                self.template_name,
                self._context(
                    request,
                    school,
                    edit_form=form,
                    show_edit_modal=True,
                    editing_membership=membership,
                ),
                status=400,
            )

        form = StaffInviteForm(
            request.POST,
            school=school,
            admin_slots_remaining=slots,
        )
        if form.is_valid():
            try:
                invitation = form.save(invited_by=request.user)
            except ValidationError as exc:
                if hasattr(exc, 'message_dict'):
                    for field, errors in exc.message_dict.items():
                        for error in errors:
                            form.add_error(
                                None if field == '__all__' else field,
                                error,
                            )
                else:
                    form.add_error(None, exc)
            else:
                from tenants.services import notify_invitee_of_invitation

                notify_invitee_of_invitation(invitation)
                messages.success(
                    request,
                    f'Invitation sent to {invitation.email}.',
                )
                return redirect('dashboard:teachers')

        messages.error(request, 'Could not create invitation. Check the form.')
        context = self._context(
            request,
            school,
            invite_form=form,
            show_invite_modal=True,
        )
        return render(request, self.template_name, context, status=400)

    def _context(
        self,
        request,
        school,
        invite_form=None,
        edit_form=None,
        show_invite_modal=False,
        show_edit_modal=False,
        editing_membership=None,
    ):
        memberships = (
            SchoolMembership.objects.filter(school=school)
            .select_related('user')
            .prefetch_related('user__assigned_streams__grade_level')
            .order_by('user__first_name', 'user__last_name', 'user__email')
        )
        staff_rows = []
        for membership in memberships:
            member = membership.user
            streams = [
                stream
                for stream in member.assigned_streams.all()
                if stream.school_id == school.pk
            ]
            staff_rows.append(
                {
                    'user': member,
                    'membership': membership,
                    'name': member.get_full_name() or member.email,
                    'role': _membership_role_label(membership),
                    'streams': streams,
                }
            )
        pending = StaffInvitation.objects.filter(
            school=school,
            status=StaffInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).order_by('-created_at')
        slots = _admin_slots_remaining(school)
        if invite_form is None:
            invite_form = StaffInviteForm(school=school, admin_slots_remaining=slots)
        if edit_form is None:
            edit_form = StaffMemberEditForm(
                school=school,
                membership=editing_membership,
                admin_slots_remaining=slots,
            )

        return {
            'page_title': 'Teachers',
            'staff_rows': staff_rows,
            'pending_invitations': pending,
            'invite_form': invite_form,
            'edit_form': edit_form,
            'admin_count': _active_admin_count(school),
            'admin_slots_remaining': slots,
            'show_invite_modal': show_invite_modal,
            'show_edit_modal': show_edit_modal,
            'editing_membership': editing_membership,
        }


@method_decorator(school_admin_required, name='dispatch')
class ClassStreamAssignmentView(LoginRequiredMixin, View):
    template_name = 'dashboard/classes.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        return render(request, self.template_name, self._context(request, school))

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        action = (request.POST.get('action') or '').strip()
        if action == 'create_grade':
            form = GradeLevelForm(request.POST, school=school)
            if form.is_valid():
                grade = form.save()
                messages.success(request, f'Created {grade.name}.')
                return redirect('dashboard:classes')
            messages.error(request, 'Could not create class. Check the form.')
            return render(
                request,
                self.template_name,
                self._context(
                    request,
                    school,
                    grade_form=form,
                    show_grade_modal=True,
                    grade_modal_mode='create',
                ),
                status=400,
            )

        if action == 'edit_grade':
            grade = get_object_or_404(
                GradeLevel,
                slug=request.POST.get('grade_slug'),
                school=school,
            )
            form = GradeLevelForm(request.POST, instance=grade, school=school)
            if form.is_valid():
                grade = form.save()
                messages.success(request, f'Updated {grade.name}.')
                return redirect('dashboard:classes')
            messages.error(request, 'Could not update class. Check the form.')
            return render(
                request,
                self.template_name,
                self._context(
                    request,
                    school,
                    grade_form=form,
                    show_grade_modal=True,
                    grade_modal_mode='edit',
                    editing_grade=grade,
                ),
                status=400,
            )

        messages.error(request, 'Unknown action.')
        return redirect('dashboard:classes')

    def _context(
        self,
        request,
        school,
        grade_form=None,
        show_grade_modal=False,
        grade_modal_mode='create',
        editing_grade=None,
    ):
        grades = list(
            GradeLevel.objects.filter(school=school)
            .select_related('class_teacher')
            .order_by('order', 'name')
        )
        streams_by_grade = {}
        for stream in (
            ClassStream.objects.filter(school=school)
            .select_related('class_teacher', 'grade_level')
            .order_by('name')
        ):
            streams_by_grade.setdefault(stream.grade_level_id, []).append(stream)

        grade_cards = []
        for grade in grades:
            streams = streams_by_grade.get(grade.pk, [])
            stream_forms = [
                {
                    'stream': stream,
                    'form': ClassTeacherAssignForm(
                        instance=stream,
                        school=school,
                        prefix=f'stream-{stream.pk}',
                    ),
                }
                for stream in streams
            ]
            grade_cards.append(
                {
                    'grade': grade,
                    'stream_forms': stream_forms,
                    'has_streams': len(streams) > 0,
                    'grade_teacher_form': GradeTeacherAssignForm(
                        instance=grade,
                        school=school,
                        prefix=f'grade-{grade.pk}',
                    ),
                    'stream_create_form': StreamCreateForm(
                        school=school,
                        grade=grade,
                        prefix=f'new-stream-{grade.pk}',
                    ),
                }
            )

        if grade_form is None:
            grade_form = GradeLevelForm(school=school)

        return {
            'page_title': 'Classes',
            'grade_cards': grade_cards,
            'grade_form': grade_form,
            'show_grade_modal': show_grade_modal,
            'grade_modal_mode': grade_modal_mode,
            'editing_grade': editing_grade,
        }


@method_decorator(school_admin_required, name='dispatch')
class GradeCreateView(LoginRequiredMixin, View):
    def get(self, request):
        return redirect('dashboard:classes')


@method_decorator(school_admin_required, name='dispatch')
class GradeEditView(LoginRequiredMixin, View):
    def get(self, request, grade_slug):
        return redirect('dashboard:classes')


@method_decorator(school_admin_required, name='dispatch')
class AssignGradeTeacherView(LoginRequiredMixin, View):
    """HTMX: assign class teacher on a grade (used when it has no streams)."""

    def post(self, request, grade_slug):
        school = _ensure_school(request)
        if school is None:
            return HttpResponseForbidden('No school context.')

        grade = get_object_or_404(GradeLevel, slug=grade_slug, school=school)
        form = GradeTeacherAssignForm(
            request.POST,
            instance=grade,
            school=school,
            prefix=f'grade-{grade.pk}',
        )
        if form.is_valid():
            updated = form.save()
            default_stream = updated.ensure_default_stream()
            if default_stream.class_teacher_id != updated.class_teacher_id:
                default_stream.class_teacher = updated.class_teacher
                default_stream.save(update_fields=['class_teacher'])
            updated.refresh_from_db()
            form = GradeTeacherAssignForm(
                instance=updated,
                school=school,
                prefix=f'grade-{updated.pk}',
            )
            return render(
                request,
                'dashboard/partials/grade_teacher_form.html',
                {'grade': updated, 'form': form, 'saved': True},
            )

        return render(
            request,
            'dashboard/partials/grade_teacher_form.html',
            {'grade': grade, 'form': form, 'saved': False},
            status=400,
        )


@method_decorator(school_admin_required, name='dispatch')
class StreamCreateView(LoginRequiredMixin, View):
    def post(self, request, grade_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        grade = get_object_or_404(GradeLevel, slug=grade_slug, school=school)
        form = StreamCreateForm(
            request.POST,
            school=school,
            grade=grade,
            prefix=f'new-stream-{grade.pk}',
        )
        if form.is_valid():
            stream = form.save()
            messages.success(
                request,
                f'Added stream {stream.name} to {grade.name}.',
            )
            return redirect('dashboard:classes')

        messages.error(request, 'Could not add stream. Check the name.')
        return redirect('dashboard:classes')


@method_decorator(school_admin_required, name='dispatch')
class AssignClassTeacherView(LoginRequiredMixin, View):
    """HTMX endpoint: update class_teacher for a stream."""

    def post(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return HttpResponseForbidden('No school context.')

        stream = get_object_or_404(ClassStream, slug=stream_slug, school=school)
        form = ClassTeacherAssignForm(
            request.POST,
            instance=stream,
            school=school,
            prefix=f'stream-{stream.pk}',
        )
        if form.is_valid():
            updated = form.save()
            updated.refresh_from_db()
            form = ClassTeacherAssignForm(
                instance=updated,
                school=school,
                prefix=f'stream-{updated.pk}',
            )
            return render(
                request,
                'dashboard/partials/stream_teacher_form.html',
                {'stream': updated, 'form': form, 'saved': True},
            )

        return render(
            request,
            'dashboard/partials/stream_teacher_form.html',
            {'stream': stream, 'form': form, 'saved': False},
            status=400,
        )


class GradeHomeView(LoginRequiredMixin, View):
    """Every class has a default stream — open its roster."""

    def get(self, request, grade_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        grade = get_object_or_404(
            GradeLevel.objects.select_related('class_teacher'),
            slug=grade_slug,
            school=school,
        )
        if not _user_can_access_grade(request, grade):
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        stream = grade.ensure_default_stream()
        if _user_can_access_stream(request, stream):
            return redirect('dashboard:stream_roster', stream_slug=stream.slug)

        for other in ClassStream.objects.filter(school=school, grade_level=grade):
            if _user_can_access_stream(request, other):
                return redirect('dashboard:stream_roster', stream_slug=other.slug)

        return redirect('dashboard:stream_roster', stream_slug=stream.slug)


def _user_can_access_grade(request, grade):
    school = getattr(request, 'school', None)
    if school is None or school.pk != grade.school_id:
        return False
    if getattr(request, 'acting_as_admin', False):
        return True
    if (
        getattr(request, 'acting_as_teacher', False)
        and grade.class_teacher_id == request.user.pk
    ):
        return True
    return False


class StreamRosterView(LoginRequiredMixin, View):
    template_name = 'dashboard/roster.html'
    table_partial = 'dashboard/partials/roster_table.html'

    def get(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class roster.')
            return redirect('dashboard:overview')

        query = (request.GET.get('q') or '').strip()
        students = Student.objects.filter(
            school=school,
            current_stream=stream,
        ).order_by('admission_number')
        if query:
            students = students.filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(admission_number__icontains=query)
            )

        context = {
            'page_title': f'Roster · {stream}',
            'stream': stream,
            'grade': stream.grade_level,
            'placement': stream,
            'placement_kind': 'stream',
            'students': students,
            'query': query,
            'roster_create_url': 'dashboard:student_create',
            'roster_import_url': 'dashboard:student_import',
            'roster_finance_url': 'dashboard:stream_finance',
            'roster_edit_url': 'dashboard:student_edit',
            'roster_search_url': 'dashboard:stream_roster',
            'roster_slug': stream.slug,
        }

        if request.headers.get('HX-Target') == 'roster-table':
            return render(request, self.table_partial, context)

        return render(request, self.template_name, context)


class StudentCreateView(LoginRequiredMixin, View):
    template_name = 'dashboard/student_form.html'

    def get(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        form = StudentForm(
            school=school,
            stream_queryset=_managed_stream_queryset(request, school),
            fixed_stream=stream,
            initial={'is_active': True},
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Add student',
                'stream': stream,
                'grade': stream.grade_level,
                'placement': stream,
                'placement_kind': 'stream',
                'form': form,
                'is_create': True,
                'roster_back_url': 'dashboard:stream_roster',
                'roster_slug': stream.slug,
            },
        )

    def post(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        form = StudentForm(
            request.POST,
            school=school,
            stream_queryset=_managed_stream_queryset(request, school),
            fixed_stream=stream,
        )
        if form.is_valid():
            student = form.save()
            messages.success(request, f'Saved {student.full_name}.')
            return redirect('dashboard:stream_roster', stream_slug=stream.slug)

        messages.error(request, 'Could not save student. Check the form.')
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Add student',
                'stream': stream,
                'grade': stream.grade_level,
                'placement': stream,
                'placement_kind': 'stream',
                'form': form,
                'is_create': True,
                'roster_back_url': 'dashboard:stream_roster',
                'roster_slug': stream.slug,
            },
            status=400,
        )


class StudentEditView(LoginRequiredMixin, View):
    template_name = 'dashboard/student_form.html'

    def get(self, request, stream_slug, admission_number):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        student = get_object_or_404(
            Student,
            school=school,
            current_stream=stream,
            admission_number=admission_number,
        )
        form = StudentForm(
            instance=student,
            school=school,
            stream_queryset=_managed_stream_queryset(request, school),
            fixed_stream=stream,
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': f'Edit · {student.full_name}',
                'stream': stream,
                'grade': stream.grade_level,
                'placement': stream,
                'placement_kind': 'stream',
                'student': student,
                'form': form,
                'is_create': False,
                'roster_back_url': 'dashboard:stream_roster',
                'roster_slug': stream.slug,
            },
        )

    def post(self, request, stream_slug, admission_number):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        student = get_object_or_404(
            Student,
            school=school,
            current_stream=stream,
            admission_number=admission_number,
        )
        form = StudentForm(
            request.POST,
            instance=student,
            school=school,
            stream_queryset=_managed_stream_queryset(request, school),
            fixed_stream=stream,
        )
        if form.is_valid():
            student = form.save()
            messages.success(request, f'Updated {student.full_name}.')
            return redirect('dashboard:stream_roster', stream_slug=stream.slug)

        messages.error(request, 'Could not update student. Check the form.')
        return render(
            request,
            self.template_name,
            {
                'page_title': f'Edit · {student.full_name}',
                'stream': stream,
                'grade': stream.grade_level,
                'placement': stream,
                'placement_kind': 'stream',
                'student': student,
                'form': form,
                'is_create': False,
                'roster_back_url': 'dashboard:stream_roster',
                'roster_slug': stream.slug,
            },
            status=400,
        )


class StudentImportView(LoginRequiredMixin, View):
    template_name = 'dashboard/student_import.html'

    def get(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        return render(
            request,
            self.template_name,
            {
                'page_title': f'Import · {stream}',
                'stream': stream,
                'grade': stream.grade_level,
                'placement': stream,
                'placement_kind': 'stream',
                'form': StudentImportUploadForm(),
                'report': None,
                'roster_back_url': 'dashboard:stream_roster',
                'roster_slug': stream.slug,
                'template_url': 'dashboard:student_import_template',
            },
        )

    def post(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        form = StudentImportUploadForm(request.POST, request.FILES)
        report = None
        if form.is_valid():
            try:
                rows = parse_student_spreadsheet(form.cleaned_data['file'])
            except ValidationError as exc:
                form.add_error('file', exc)
            else:
                if not rows:
                    form.add_error('file', 'No data rows found in the spreadsheet.')
                else:
                    report = import_students_for_stream(
                        school=school,
                        stream=stream,
                        rows=rows,
                        acting_as_admin=bool(
                            getattr(request, 'acting_as_admin', False)
                        ),
                    )
                    messages.success(
                        request,
                        (
                            f'Import finished: {report.created} created, '
                            f'{report.updated} updated, {report.skipped} skipped.'
                        ),
                    )
        else:
            messages.error(request, 'Could not process the upload.')

        return render(
            request,
            self.template_name,
            {
                'page_title': f'Import · {stream}',
                'stream': stream,
                'grade': stream.grade_level,
                'placement': stream,
                'placement_kind': 'stream',
                'form': form,
                'report': report,
                'roster_back_url': 'dashboard:stream_roster',
                'roster_slug': stream.slug,
                'template_url': 'dashboard:student_import_template',
            },
            status=400 if report is None and form.errors else 200,
        )


class StudentImportTemplateView(LoginRequiredMixin, View):
    def get(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = (
            f'attachment; filename="kora-{stream.slug}-students-template.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(TEMPLATE_HEADERS)
        writer.writerow(
            [
                'GF-2026-001',
                'Amina',
                'Otieno',
                'Jane Otieno',
                '+254712345678',
                'yes',
            ]
        )
        return response


class StreamFinanceView(LoginRequiredMixin, View):
    template_name = 'finance/stream_finance.html'

    def get(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class finance.')
            return redirect('dashboard:overview')

        return self._render(request, school, stream)

    def post(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class finance.')
            return redirect('dashboard:overview')

        action = request.POST.get('action', 'payment')
        if action == 'create_charge':
            return self._create_charge(request, school, stream)

        form = ManualPaymentForm(request.POST)
        admission = (request.POST.get('admission_number') or '').strip()
        student = get_object_or_404(
            Student,
            school=school,
            current_stream=stream,
            admission_number=admission,
        )
        student_fee = (
            StudentFee.objects.filter(school=school, student=student)
            .exclude(status__in=[StudentFee.Status.WAIVED, StudentFee.Status.PAID])
            .order_by('created_at')
            .first()
        )
        if student_fee is None:
            student_fee = (
                StudentFee.objects.filter(school=school, student=student)
                .exclude(status=StudentFee.Status.WAIVED)
                .order_by('-created_at')
                .first()
            )
        if student_fee is None:
            messages.error(
                request,
                'No open fee assigned to this student. Add a new charge first.',
            )
            return redirect('dashboard:stream_finance', stream_slug=stream.slug)

        if form.is_valid():
            Payment.objects.create(
                school=school,
                student_fee=student_fee,
                amount=form.cleaned_data['amount'],
                method=Payment.Method.MANUAL,
                recorded_by=request.user,
                note=form.cleaned_data.get('note') or '',
                paid_at=timezone.now(),
            )
            messages.success(
                request,
                f'Recorded payment for {student.full_name}.',
            )
            return redirect('dashboard:stream_finance', stream_slug=stream.slug)

        messages.error(request, 'Could not record payment. Check the amount.')
        return self._render(
            request,
            school,
            stream,
            payment_form=form,
            selected_admission=admission,
            status=400,
        )

    def _create_charge(self, request, school, stream):
        form = FeeChargeForm(request.POST)
        if not form.is_valid():
            messages.error(request, 'Could not create charge. Check the details.')
            return self._render(
                request,
                school,
                stream,
                charge_form=form,
                show_charge_modal=True,
                status=400,
            )

        fee, assigned = create_and_assign_charge(
            school=school,
            name=form.cleaned_data['name'],
            amount=form.cleaned_data['amount'],
            due_date=form.cleaned_data.get('due_date'),
            description=form.cleaned_data.get('description') or '',
            streams=[stream],
            created_by=request.user,
        )
        if assigned == 0:
            messages.warning(
                request,
                f'Charge “{fee.name}” was created but this class has no active students.',
            )
        else:
            messages.success(
                request,
                f'Charge “{fee.name}” assigned to {assigned} student'
                f'{"s" if assigned != 1 else ""} in {stream}.',
            )
        return redirect('dashboard:stream_finance', stream_slug=stream.slug)

    def _render(
        self,
        request,
        school,
        stream,
        *,
        payment_form=None,
        charge_form=None,
        selected_admission='',
        show_charge_modal=False,
        status=200,
    ):
        rows, due, paid, balance = stream_finance_rows(school, stream)
        active_count = Student.objects.filter(
            school=school,
            current_stream=stream,
            is_active=True,
        ).count()
        return render(
            request,
            self.template_name,
            {
                'page_title': f'Finance · {stream}',
                'stream': stream,
                'rows': rows,
                'total_due': due,
                'total_paid': paid,
                'total_balance': balance,
                'payment_form': payment_form or ManualPaymentForm(),
                'charge_form': charge_form or FeeChargeForm(),
                'selected_admission': selected_admission,
                'show_charge_modal': show_charge_modal,
                'chargeable_student_count': active_count,
                'placement_kind': 'stream',
                'roster_back_url': 'dashboard:stream_roster',
                'roster_slug': stream.slug,
                'finance_post_url': 'dashboard:stream_finance',
            },
            status=status,
        )


def _redirect_to_grade_default_stream(request, school, grade_slug, target):
    """Resolve a grade's default stream and redirect to a stream-scoped view."""
    grade = get_object_or_404(
        GradeLevel.objects.select_related('class_teacher'),
        slug=grade_slug,
        school=school,
    )
    if not _user_can_access_grade(request, grade):
        messages.error(request, 'You do not have access to this class.')
        return redirect('dashboard:overview')
    stream = grade.ensure_default_stream()
    return redirect(target, stream_slug=stream.slug)


class GradeStudentCreateView(LoginRequiredMixin, View):
    def get(self, request, grade_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        return _redirect_to_grade_default_stream(
            request, school, grade_slug, 'dashboard:student_create'
        )

    def post(self, request, grade_slug):
        return self.get(request, grade_slug)


class GradeStudentEditView(LoginRequiredMixin, View):
    def get(self, request, grade_slug, admission_number):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        grade = get_object_or_404(GradeLevel, slug=grade_slug, school=school)
        stream = grade.ensure_default_stream()
        return redirect(
            'dashboard:student_edit',
            stream_slug=stream.slug,
            admission_number=admission_number,
        )

    def post(self, request, grade_slug, admission_number):
        return self.get(request, grade_slug, admission_number)


class GradeStudentImportView(LoginRequiredMixin, View):
    def get(self, request, grade_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        return _redirect_to_grade_default_stream(
            request, school, grade_slug, 'dashboard:student_import'
        )

    def post(self, request, grade_slug):
        return self.get(request, grade_slug)


class GradeStudentImportTemplateView(LoginRequiredMixin, View):
    def get(self, request, grade_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        return _redirect_to_grade_default_stream(
            request, school, grade_slug, 'dashboard:student_import_template'
        )


class GradeFinanceView(LoginRequiredMixin, View):
    def get(self, request, grade_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        return _redirect_to_grade_default_stream(
            request, school, grade_slug, 'dashboard:stream_finance'
        )

    def post(self, request, grade_slug):
        return self.get(request, grade_slug)
