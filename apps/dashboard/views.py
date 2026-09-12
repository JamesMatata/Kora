import csv
import json
from datetime import datetime
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

from academics.models import AcademicYear, AttendanceRecord, ClassStream, GradeLevel, Student
from communications.models import ConversationSession, MessageLog, ParentContact
from communications.services.twilio_service import send_whatsapp_message
from dashboard.forms import (
    ClassTeacherAssignForm,
    GradeLevelForm,
    GradeTeacherAssignForm,
    ManualPaymentForm,
    StaffInviteForm,
    StaffMemberEditForm,
    StreamCreateForm,
    StreamEditForm,
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
from finance.services.daraja import DarajaError, register_c2b_urls
from finance.services.receipts import (
    render_receipt_pdf,
    send_payment_receipt_whatsapp,
)
from tenants.decorators import (
    school_admin_required,
    school_finance_required,
    school_staff_required,
)
from tenants.forms import SchoolSettingsForm
from tenants.models import SchoolMembership, StaffInvitation

User = get_user_model()


def _parse_attendance_date(raw: str):
    from communications.services.whatsapp_delivery import local_today

    raw = (raw or '').strip()
    if not raw:
        return local_today()
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return local_today()


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
    return membership.role_label


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
                    'weekly_update_streams': [],
                    'weekly_update_window': False,
                }
            )
            from academics.services.weekly_updates import (
                is_weekly_update_window,
                streams_needing_weekly_update,
            )

            context['weekly_update_window'] = is_weekly_update_window()
            context['weekly_update_streams'] = streams_needing_weekly_update(
                school,
                request.user,
            )
            return context

        staff = SchoolMembership.objects.filter(school=school)
        students = Student.objects.filter(school=school, is_active=True)
        streams = ClassStream.objects.filter(school=school)

        invoice_qs = FeeInvoice.objects.filter(school=school)
        year = AcademicYear.objects.filter(school=school, is_current=True).first()
        finance_scope_label = 'All time'
        if year is not None:
            # Prefer invoices whose term text references the current year name.
            year_invoices = invoice_qs.filter(term__icontains=year.name)
            if year_invoices.exists():
                invoice_qs = year_invoices
                finance_scope_label = f'This year ({year.name})'

        aggregates = invoice_qs.aggregate(
            gross=Coalesce(Sum('total_amount'), Decimal('0.00')),
            discounts=Coalesce(Sum('discount_amount'), Decimal('0.00')),
        )
        total_billed_gross = aggregates['gross'] or Decimal('0.00')
        total_discounts = aggregates['discounts'] or Decimal('0.00')
        total_billed = max(total_billed_gross - total_discounts, Decimal('0.00'))

        invoice_ids = list(invoice_qs.values_list('pk', flat=True))
        total_collected = (
            PaymentTransaction.objects.filter(
                school=school,
                status=PaymentTransaction.Status.SUCCESS,
                invoice_id__in=invoice_ids,
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
                'total_billed_gross': total_billed_gross,
                'total_discounts': total_discounts,
                'total_collected': total_collected,
                'outstanding_balance': outstanding_balance,
                'collection_rate': collection_rate,
                'finance_scope_label': finance_scope_label,
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
    view = (request.GET.get('view') or 'all').strip().lower() or 'all'
    if view not in ('all', 'overdue', 'promised', 'paused'):
        view = 'all'
    return {
        'q': (request.GET.get('q') or '').strip(),
        'grade': (request.GET.get('grade') or '').strip(),
        'stream': (request.GET.get('stream') or '').strip(),
        'status': (request.GET.get('status') or 'ALL').strip().upper() or 'ALL',
        'view': view,
    }


def _fee_ledger_queryset(school, *, q='', grade='', stream='', status='ALL', view='all'):
    from communications.services.whatsapp_delivery import local_today

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

    today = local_today()
    if view == 'overdue':
        qs = qs.exclude(status=FeeInvoice.Status.PAID).filter(due_date__lte=today)
    elif view == 'promised':
        qs = qs.filter(
            promises__status=PaymentPromise.Status.PENDING,
        ).distinct()
    elif view == 'paused':
        from communications.models import ParentContact
        from communications.services.identity import normalize_incoming_phone

        paused_phones = list(
            ParentContact.objects.filter(
                school=school,
                reminders_paused_at__isnull=False,
            ).values_list('phone_number', flat=True)
        )
        # Match students whose parent_phone normalizes into paused set.
        candidate_ids = []
        unpaid = qs.exclude(status=FeeInvoice.Status.PAID).select_related('student')
        paused_set = set(paused_phones)
        for inv in unpaid[:2000]:
            raw = (inv.student.parent_phone or '').strip()
            if not raw:
                continue
            try:
                if normalize_incoming_phone(raw) in paused_set:
                    candidate_ids.append(inv.pk)
            except ValueError:
                continue
        qs = qs.filter(pk__in=candidate_ids)
    elif status and status != 'ALL':
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
    from communications.models import ParentContact
    from communications.services.identity import normalize_incoming_phone
    from communications.services.whatsapp_delivery import local_today

    rows = []
    phones = {
        (inv.student.parent_phone or '').strip()
        for inv in invoices
        if (inv.student.parent_phone or '').strip()
    }
    paused_phones: set[str] = set()
    paused_by_phone: dict[str, object] = {}
    if phones:
        normalized = []
        for phone in phones:
            try:
                normalized.append(normalize_incoming_phone(phone))
            except ValueError:
                continue
        if normalized:
            for parent in ParentContact.objects.filter(
                school=school,
                phone_number__in=normalized,
                reminders_paused_at__isnull=False,
            ):
                paused_phones.add(parent.phone_number)
                paused_by_phone[parent.phone_number] = parent

    invoice_ids = [inv.pk for inv in invoices]
    promises_by_invoice = {}
    if invoice_ids:
        for promise in (
            PaymentPromise.objects.filter(
                school=school,
                invoice_id__in=invoice_ids,
                status=PaymentPromise.Status.PENDING,
            )
            .order_by('promised_date', '-created_at')
        ):
            promises_by_invoice.setdefault(promise.invoice_id, promise)

    today = local_today()
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
        elif student.grade_level_id:
            profile_url = reverse(
                'dashboard:grade_student_edit',
                kwargs={
                    'grade_slug': student.grade_level.slug,
                    'admission_number': student.admission_number,
                },
            )
        statement_url = reverse(
            'finance:student_fee_statement',
            kwargs={'admission_number': student.admission_number},
        )
        reminders_paused = False
        paused_parent = None
        raw_phone = (student.parent_phone or '').strip()
        if raw_phone:
            try:
                norm = normalize_incoming_phone(raw_phone)
                reminders_paused = norm in paused_phones
                paused_parent = paused_by_phone.get(norm)
            except ValueError:
                reminders_paused = False
        promise = promises_by_invoice.get(invoice.pk)
        promise_overdue = bool(
            promise is not None and promise.promised_date < today
        )
        rows.append(
            {
                'invoice': invoice,
                'student': student,
                'balance': invoice.balance,
                'conversation': conversation,
                'profile_url': profile_url,
                'statement_url': statement_url,
                'reminders_paused': reminders_paused,
                'paused_parent': paused_parent,
                'promise': promise,
                'promise_overdue': promise_overdue,
            }
        )
    return rows


@method_decorator(school_finance_required, name='dispatch')
class FeeLedgerView(LoginRequiredMixin, View):
    """Collections workbench: invoices, overdue, promises, paused reminders."""

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
                'page_title': 'Finance',
                'filters': filters,
                'collection_view': filters['view'],
                'status_choices': [
                    ('ALL', 'All statuses'),
                    *FeeInvoice.Status.choices,
                ],
                'grades': grades,
                'streams': streams,
                'streams_by_grade_json': json.dumps(streams_by_grade),
                'ledger_rows': _ledger_row_context(school, invoices),
                'query': filters['q'],
                'can_manage_promises': True,
            },
        )


@method_decorator(school_finance_required, name='dispatch')
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
                'collection_view': filters['view'],
                'can_manage_promises': True,
            },
        )


@method_decorator(school_finance_required, name='dispatch')
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
            return redirect('finance:fee_ledger')

        try:
            amount = Decimal(amount_raw)
        except Exception:
            messages.error(request, 'Enter a valid amount.')
            return redirect('finance:fee_ledger')

        if amount <= 0:
            messages.error(request, 'Amount must be greater than zero.')
            return redirect('finance:fee_ledger')

        if amount > invoice.balance:
            messages.info(
                request,
                (
                    'Amount is above this invoice balance. After payment, excess '
                    'will clear other open fees first, then be held as credit.'
                ),
            )

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
            return redirect('finance:fee_ledger')

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
        return redirect('finance:fee_ledger')


def _defaulter_queryset(school, *, q='', grade='', stream=''):
    from communications.services.whatsapp_delivery import local_today

    today = local_today()
    qs = (
        FeeInvoice.objects.filter(school=school)
        .exclude(status=FeeInvoice.Status.PAID)
        .filter(due_date__lte=today)
        .select_related(
            'student',
            'student__grade_level',
            'student__current_stream',
        )
        .order_by('due_date', 'student__admission_number')
    )
    if grade:
        qs = qs.filter(student__grade_level_id=grade)
    if stream:
        qs = qs.filter(student__current_stream_id=stream)
    if q:
        qs = qs.filter(
            Q(student__first_name__icontains=q)
            | Q(student__last_name__icontains=q)
            | Q(student__admission_number__icontains=q)
        )
    return qs


@method_decorator(school_finance_required, name='dispatch')
class PaymentReceiptView(LoginRequiredMixin, View):
    """HTML receipt for a successful payment (print / Save as PDF)."""

    template_name = 'dashboard/payment_receipt.html'

    def get(self, request, payment_id):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        payment_tx = get_object_or_404(
            PaymentTransaction.objects.select_related(
                'invoice',
                'invoice__student',
                'invoice__student__current_stream',
                'invoice__student__grade_level',
                'school',
            ),
            school=school,
            pk=payment_id,
            status=PaymentTransaction.Status.SUCCESS,
        )
        student = payment_tx.invoice.student
        parent_paused = False
        to_phone = (payment_tx.phone_number or student.parent_phone or '').strip()
        if to_phone:
            try:
                from communications.services.identity import resolve_parent_identity

                parent = resolve_parent_identity(school, to_phone)
                parent_paused = parent.reminders_paused_at is not None
            except ValueError:
                parent_paused = False
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Payment receipt',
                'payment': payment_tx,
                'invoice': payment_tx.invoice,
                'student': student,
                'receipt_whatsapp_sent_at': payment_tx.receipt_whatsapp_sent_at,
                'parent_whatsapp_paused': parent_paused,
            },
        )


@method_decorator(school_finance_required, name='dispatch')
class PaymentReceiptPdfView(LoginRequiredMixin, View):
    """Download PDF receipt for a successful payment."""

    def get(self, request, payment_id):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        payment_tx = get_object_or_404(
            PaymentTransaction.objects.select_related(
                'invoice',
                'invoice__student',
                'invoice__student__current_stream',
                'invoice__student__grade_level',
                'school',
            ),
            school=school,
            pk=payment_id,
            status=PaymentTransaction.Status.SUCCESS,
        )
        pdf_bytes = render_receipt_pdf(payment_tx)
        receipt_no = payment_tx.mpesa_receipt_number or f'TX-{payment_tx.pk}'
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="receipt-{receipt_no}.pdf"'
        )
        return response


@method_decorator(school_finance_required, name='dispatch')
class PaymentReceiptWhatsAppView(LoginRequiredMixin, View):
    """Resend WhatsApp receipt for a successful payment."""

    def post(self, request, payment_id):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        payment_tx = get_object_or_404(
            PaymentTransaction.objects.select_related(
                'invoice',
                'invoice__student',
                'school',
            ),
            school=school,
            pk=payment_id,
            status=PaymentTransaction.Status.SUCCESS,
        )
        ok, detail = send_payment_receipt_whatsapp(payment_tx, force=True)
        if ok:
            messages.success(request, 'Receipt sent on WhatsApp.')
        else:
            messages.error(request, detail or 'Could not send WhatsApp receipt.')
        return redirect('finance:payment_receipt', payment_id=payment_tx.pk)


@method_decorator(school_finance_required, name='dispatch')
class StudentFeeStatementView(LoginRequiredMixin, View):
    """Per-student fee statement: invoices + successful payments."""

    template_name = 'dashboard/student_fee_statement.html'

    def get(self, request, admission_number):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        student = get_object_or_404(
            Student.objects.select_related('grade_level', 'current_stream'),
            school=school,
            admission_number=admission_number,
        )
        invoices = list(
            FeeInvoice.objects.filter(school=school, student=student)
            .prefetch_related('line_items')
            .order_by(
                '-due_date',
                '-created_at',
            )
        )
        payments = list(
            PaymentTransaction.objects.filter(
                school=school,
                invoice__student=student,
                status=PaymentTransaction.Status.SUCCESS,
            )
            .select_related('invoice')
            .order_by('-created_at')
        )
        from finance.models import StudentFeeCredit

        fee_credit = StudentFeeCredit.objects.filter(
            school=school, student=student
        ).first()
        total_billed = sum((inv.total_amount for inv in invoices), Decimal('0.00'))
        total_discount = sum(
            (inv.discount_amount for inv in invoices), Decimal('0.00')
        )
        total_paid = sum((inv.paid_amount for inv in invoices), Decimal('0.00'))
        total_balance = sum((inv.balance for inv in invoices), Decimal('0.00'))
        profile_url = ''
        if student.current_stream_id:
            profile_url = reverse(
                'dashboard:student_edit',
                kwargs={
                    'stream_slug': student.current_stream.slug,
                    'admission_number': student.admission_number,
                },
            )
        elif student.grade_level_id:
            profile_url = reverse(
                'dashboard:grade_student_edit',
                kwargs={
                    'grade_slug': student.grade_level.slug,
                    'admission_number': student.admission_number,
                },
            )

        return render(
            request,
            self.template_name,
            {
                'page_title': f'Fee statement · {student.full_name}',
                'student': student,
                'invoices': invoices,
                'payments': payments,
                'fee_credit': fee_credit,
                'total_billed': total_billed,
                'total_discount': total_discount,
                'total_paid': total_paid,
                'total_balance': total_balance,
                'profile_url': profile_url,
            },
        )

    def post(self, request, admission_number):
        """Credit refund actions (admin/bursar)."""
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        student = get_object_or_404(
            Student,
            school=school,
            admission_number=admission_number,
        )
        from finance.models import StudentFeeCredit
        from finance.services.credits import (
            allow_credit_refund,
            mark_credit_refund_collected,
            notify_parent_refund_visit,
        )
        from tenants.audit import log_audit_event
        from tenants.models import AuditEvent

        credit = StudentFeeCredit.objects.filter(
            school=school, student=student
        ).first()
        action = (request.POST.get('action') or '').strip()
        if credit is None or credit.balance <= 0:
            messages.error(request, 'No fee credit available for this student.')
            return redirect(
                'finance:student_fee_statement',
                admission_number=admission_number,
            )

        if action == 'allow_refund':
            allow_credit_refund(credit=credit, actor=request.user)
            ok, detail = notify_parent_refund_visit(credit=credit)
            log_audit_event(
                school,
                category=AuditEvent.Category.PAYMENT,
                action='fee_credit_refund_allowed',
                summary=f'Refund allowed for {student.admission_number}: {credit.balance}',
                actor=request.user,
                object_type='StudentFeeCredit',
                object_id=str(credit.pk),
            )
            if ok:
                messages.success(
                    request,
                    'Refund allowed. Parent was asked to visit the school office.',
                )
            else:
                messages.warning(
                    request,
                    f'Refund allowed, but WhatsApp notice failed: {detail}',
                )
        elif action == 'mark_refund_collected':
            mark_credit_refund_collected(credit=credit, actor=request.user)
            log_audit_event(
                school,
                category=AuditEvent.Category.PAYMENT,
                action='fee_credit_refund_collected',
                summary=f'Refund collected for {student.admission_number}',
                actor=request.user,
                object_type='StudentFeeCredit',
                object_id=str(credit.pk),
            )
            messages.success(request, 'Refund marked as collected at the school.')
        else:
            messages.error(request, 'Unknown credit action.')
        return redirect(
            'finance:student_fee_statement',
            admission_number=admission_number,
        )


@method_decorator(school_finance_required, name='dispatch')
class InvoiceManualPaymentView(LoginRequiredMixin, View):
    """Record cash/bank payment against a FeeInvoice (replaces legacy stream cash)."""

    def post(self, request, admission_number):
        from uuid import uuid4

        from communications.services.identity import normalize_incoming_phone
        from finance.services.credits import apply_payment_across_invoices
        from tenants.audit import log_audit_event
        from tenants.models import AuditEvent

        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        student = get_object_or_404(
            Student, school=school, admission_number=admission_number
        )
        invoice_id = (request.POST.get('invoice_id') or '').strip()
        amount_raw = (request.POST.get('amount') or '').strip()
        note = (request.POST.get('note') or '').strip()[:200]

        invoice = get_object_or_404(
            FeeInvoice, school=school, student=student, pk=invoice_id
        )
        try:
            amount = Decimal(amount_raw)
        except Exception:
            messages.error(request, 'Enter a valid amount.')
            return redirect(
                'finance:student_fee_statement',
                admission_number=admission_number,
            )
        if amount <= 0:
            messages.error(request, 'Amount must be greater than zero.')
            return redirect(
                'finance:student_fee_statement',
                admission_number=admission_number,
            )

        try:
            phone = normalize_incoming_phone(student.parent_phone or '')
        except ValueError:
            phone = '+254700000000'

        payment_tx = PaymentTransaction.objects.create(
            school=school,
            invoice=invoice,
            phone_number=phone,
            amount=amount,
            mpesa_receipt_number=f'MANUAL-{uuid4().hex[:12].upper()}',
            status=PaymentTransaction.Status.SUCCESS,
            result_code=0,
            result_desc=(
                f'Manual cash/bank payment{(": " + note) if note else ""}'
            ),
            raw_callback_payload={'source': 'manual', 'note': note},
        )
        allocation = apply_payment_across_invoices(
            school=school,
            student=student,
            amount=amount,
            payment_tx=payment_tx,
            primary_invoice=invoice,
        )
        log_audit_event(
            school,
            category=AuditEvent.Category.PAYMENT,
            action='manual_invoice_payment',
            summary=(
                f'Manual payment {amount} for {student.admission_number}'
                + (f' ({note})' if note else '')
            ),
            actor=request.user,
            object_type='PaymentTransaction',
            object_id=str(payment_tx.pk),
            metadata={
                'amount': str(amount),
                'credit_added': str(allocation.credit_added),
            },
        )
        msg = f'Recorded payment of KES {amount}.'
        if allocation.credit_added:
            msg += f' KES {allocation.credit_added} held as credit.'
        messages.success(request, msg)
        return redirect(
            'finance:student_fee_statement',
            admission_number=admission_number,
        )


@method_decorator(school_finance_required, name='dispatch')
class InvoiceDiscountView(LoginRequiredMixin, View):
    """Apply bursary/discount or waive remaining balance on an invoice."""

    def post(self, request, admission_number):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        student = get_object_or_404(
            Student,
            school=school,
            admission_number=admission_number,
        )
        invoice = get_object_or_404(
            FeeInvoice,
            school=school,
            student=student,
            pk=request.POST.get('invoice_id'),
        )
        note = (request.POST.get('note') or '').strip()
        waive = (request.POST.get('waive_remaining') or '') == '1'
        try:
            amount = Decimal((request.POST.get('amount') or '0').strip() or '0')
        except Exception:
            messages.error(request, 'Enter a valid discount amount.')
            return redirect(
                'finance:student_fee_statement',
                admission_number=student.admission_number,
            )

        try:
            invoice.apply_discount(amount, note=note, waive_remaining=waive)
        except ValidationError as exc:
            messages.error(request, str(exc))
            return redirect(
                'finance:student_fee_statement',
                admission_number=student.admission_number,
            )

        from tenants.audit import log_audit_event
        from tenants.models import AuditEvent

        log_audit_event(
            school,
            category=AuditEvent.Category.PAYMENT,
            action='invoice_discount' if not waive else 'invoice_waiver',
            summary=(
                f'{"Waiver" if waive else "Discount"} on {student.admission_number} '
                f'({invoice.term}): {invoice.discount_amount}'
            ),
            actor=request.user,
            object_type='FeeInvoice',
            object_id=str(invoice.pk),
            metadata={
                'discount_amount': str(invoice.discount_amount),
                'note': invoice.discount_note,
                'waive': waive,
            },
        )
        messages.success(
            request,
            'Waiver applied.' if waive else 'Discount / bursary applied.',
        )
        return redirect(
            'finance:student_fee_statement',
            admission_number=student.admission_number,
        )


@method_decorator(school_finance_required, name='dispatch')
class DefaulterReportView(LoginRequiredMixin, View):
    """Legacy URL — collections Overdue tab is the workbench now."""

    def get(self, request):
        params = request.GET.copy()
        params['view'] = 'overdue'
        return redirect(f"{reverse('finance:fee_ledger')}?{params.urlencode()}")


@method_decorator(school_finance_required, name='dispatch')
class DefaulterExportView(LoginRequiredMixin, View):
    """CSV export of overdue fee invoices."""

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        filters = {
            'q': (request.GET.get('q') or '').strip(),
            'grade': (request.GET.get('grade') or '').strip(),
            'stream': (request.GET.get('stream') or '').strip(),
        }
        invoices = _defaulter_queryset(school, **filters)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = (
            'attachment; filename="fee-defaulters.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                'Admission',
                'Student',
                'Class',
                'Parent phone',
                'Term',
                'Due date',
                'Billed',
                'Paid',
                'Balance',
                'Status',
            ]
        )
        for invoice in invoices.iterator():
            student = invoice.student
            writer.writerow(
                [
                    student.admission_number,
                    student.full_name,
                    str(student.current_stream or student.grade_level or ''),
                    student.parent_phone or '',
                    invoice.term,
                    invoice.due_date.isoformat() if invoice.due_date else '',
                    invoice.total_amount,
                    invoice.paid_amount,
                    invoice.balance,
                    invoice.status,
                ]
            )
        return response


@method_decorator(school_staff_required, name='dispatch')
class PausedRemindersView(LoginRequiredMixin, View):
    """Parents who opted out of automated fee reminders (STOP)."""

    template_name = 'dashboard/paused_reminders.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        # Finance workbench owns this for staff acting as admin/bursar.
        # Dual-role users in Teacher mode keep this dedicated page.
        if getattr(request, 'acting_as_admin', False) or getattr(
            request, 'acting_as_bursar', False
        ):
            return redirect(f"{reverse('finance:fee_ledger')}?view=paused")

        can_resume = bool(
            getattr(request, 'acting_as_admin', False)
            or getattr(request, 'acting_as_bursar', False)
        )
        parents = list(
            ParentContact.objects.filter(
                school=school,
                reminders_paused_at__isnull=False,
            )
            .prefetch_related('students')
            .order_by('-reminders_paused_at')
        )
        rows = []
        for parent in parents:
            students = list(parent.students.filter(school=school, is_active=True)[:8])
            rows.append(
                {
                    'parent': parent,
                    'students': students,
                    'reason_label': (
                        'Parent STOP'
                        if parent.reminders_paused_reason == 'parent_stop'
                        else (
                            'Staff pause'
                            if parent.reminders_paused_reason == 'staff_pause'
                            else (parent.reminders_paused_reason or 'Paused')
                        )
                    ),
                }
            )
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Paused reminders',
                'rows': rows,
                'can_resume': can_resume,
            },
        )

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        can_resume = bool(
            getattr(request, 'acting_as_admin', False)
            or getattr(request, 'acting_as_bursar', False)
        )
        if not can_resume:
            messages.error(
                request,
                'Only an administrator or bursar can resume fee reminders.',
            )
            next_url = (request.POST.get('next') or '').strip()
            if next_url.startswith('/'):
                return redirect(next_url)
            return redirect('finance:paused_reminders')

        parent_id = (request.POST.get('parent_id') or '').strip()
        parent = get_object_or_404(
            ParentContact,
            pk=parent_id,
            school=school,
        )
        if parent.reminders_paused_at is None:
            messages.info(request, 'Reminders are already active for that parent.')
            next_url = (request.POST.get('next') or '').strip()
            if next_url.startswith('/'):
                return redirect(next_url)
            return redirect('finance:paused_reminders')

        from tenants.audit import log_audit_event
        from tenants.models import AuditEvent

        parent.reminders_paused_at = None
        parent.reminders_paused_reason = ''
        parent.save(
            update_fields=[
                'reminders_paused_at',
                'reminders_paused_reason',
                'updated_at',
            ]
        )
        log_audit_event(
            school,
            category=AuditEvent.Category.OTHER,
            action='fee_reminders_resumed',
            summary=f'Staff resumed fee reminders ({parent.phone_number})',
            actor=request.user,
            object_type='ParentContact',
            object_id=str(parent.pk),
            metadata={'source': 'staff_resume', 'phone': parent.phone_number},
        )
        messages.success(
            request,
            f'Reminders resumed for {parent.parent_name or parent.phone_number}.',
        )
        next_url = (request.POST.get('next') or '').strip()
        if next_url.startswith('/'):
            return redirect(next_url)
        return redirect('finance:paused_reminders')


@method_decorator(school_finance_required, name='dispatch')
class PaymentPromisesView(LoginRequiredMixin, View):
    """Promise create/status updates (list UI lives on Collections → Promised)."""

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        params = request.GET.copy()
        params['view'] = 'promised'
        return redirect(f"{reverse('finance:fee_ledger')}?{params.urlencode()}")

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        def _done():
            next_url = (request.POST.get('next') or '').strip()
            if next_url.startswith('/'):
                return redirect(next_url)
            return redirect(f"{reverse('finance:fee_ledger')}?view=promised")

        action = (request.POST.get('action') or '').strip()

        if action == 'create':
            invoice_id = (request.POST.get('invoice_id') or '').strip()
            invoice = get_object_or_404(FeeInvoice, school=school, pk=invoice_id)
            try:
                amount = Decimal((request.POST.get('promised_amount') or '').strip())
                promised_date = datetime.strptime(
                    (request.POST.get('promised_date') or '').strip(),
                    '%Y-%m-%d',
                ).date()
            except Exception:
                messages.error(request, 'Enter a valid amount and date.')
                return _done()
            if amount <= 0:
                messages.error(request, 'Amount must be greater than zero.')
                return _done()
            PaymentPromise.objects.create(
                school=school,
                invoice=invoice,
                promised_amount=amount,
                promised_date=promised_date,
                status=PaymentPromise.Status.PENDING,
            )
            messages.success(request, 'Payment promise recorded.')
            return _done()

        promise = get_object_or_404(
            PaymentPromise,
            school=school,
            pk=request.POST.get('promise_id'),
        )
        if action == 'honor':
            promise.status = PaymentPromise.Status.HONORED
            promise.save(update_fields=['status'])
            messages.success(request, 'Promise marked as honored.')
        elif action == 'break':
            promise.status = PaymentPromise.Status.BROKEN
            promise.save(update_fields=['status'])
            messages.success(request, 'Promise marked as broken.')
        elif action == 'reopen':
            promise.status = PaymentPromise.Status.PENDING
            promise.save(update_fields=['status'])
            messages.success(request, 'Promise reopened as pending.')
        else:
            messages.error(request, 'Unknown action.')

        return _done()


@method_decorator(school_admin_required, name='dispatch')
class StudentsExportView(LoginRequiredMixin, View):
    """CSV export of active students (optional grade/stream filters)."""

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        grade = (request.GET.get('grade') or '').strip()
        stream = (request.GET.get('stream') or '').strip()
        qs = (
            Student.objects.filter(school=school, is_active=True)
            .select_related('grade_level', 'current_stream')
            .order_by('admission_number')
        )
        if grade:
            qs = qs.filter(grade_level_id=grade)
        if stream:
            qs = qs.filter(current_stream_id=stream)

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="students.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                'Admission',
                'First name',
                'Last name',
                'Grade',
                'Stream',
                'Parent name',
                'Parent phone',
            ]
        )
        for student in qs.iterator():
            writer.writerow(
                [
                    student.admission_number,
                    student.first_name,
                    student.last_name,
                    student.grade_level.name if student.grade_level_id else '',
                    student.current_stream.name if student.current_stream_id else '',
                    student.parent_name,
                    student.parent_phone,
                ]
            )
        return response


@method_decorator(school_finance_required, name='dispatch')
class PaymentsExportView(LoginRequiredMixin, View):
    """CSV export of successful M-Pesa / fee payments."""

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        qs = (
            PaymentTransaction.objects.filter(
                school=school,
                status=PaymentTransaction.Status.SUCCESS,
            )
            .select_related(
                'invoice',
                'invoice__student',
                'invoice__student__current_stream',
            )
            .order_by('-created_at')
        )
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="fee-payments.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                'Date',
                'Receipt',
                'Admission',
                'Student',
                'Class',
                'Term',
                'Amount',
                'Phone',
            ]
        )
        for payment in qs.iterator():
            student = payment.invoice.student
            writer.writerow(
                [
                    timezone.localtime(payment.created_at).strftime('%Y-%m-%d %H:%M'),
                    payment.mpesa_receipt_number or '',
                    student.admission_number,
                    student.full_name,
                    str(student.current_stream or student.grade_level or ''),
                    payment.invoice.term,
                    payment.amount,
                    payment.phone_number,
                ]
            )
        return response


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
    status_labels = {
        ConversationSession.Status.BOT_ACTIVE: 'Bot',
        ConversationSession.Status.ESCALATED_PENDING: 'Needs you',
        ConversationSession.Status.STAFF_ACTIVE: 'Staff',
        ConversationSession.Status.CLOSED: 'Closed',
    }
    rows = []
    for session in sessions:
        last_msg = (
            MessageLog.objects.filter(school=school, session=session)
            .order_by('-created_at')
            .first()
        )
        student = session.active_student
        phone = session.parent_contact.phone_number
        parent_name = (session.parent_contact.parent_name or '').strip()
        if student is not None:
            title = student.full_name
            detail = phone
        elif parent_name and parent_name != phone:
            title = parent_name
            detail = phone
        else:
            title = phone
            detail = ''
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
                'title': title,
                'detail': detail,
                'status_label': status_labels.get(session.status, session.status),
                'excerpt': excerpt or 'No messages yet',
                'timestamp': session.last_message_at or session.created_at,
            }
        )
    return rows


def _chat_console_context(request, school, *, tab='escalated', session=None):
    search_q = (request.GET.get('q') or '').strip()
    base_qs = _chat_session_queryset(school).order_by(
        '-last_message_at',
        '-created_at',
    )
    filtered_qs, active_tab = _chat_tab_filter(base_qs, tab)
    if search_q:
        # Search open threads across tabs so a wrong tab doesn't hide matches.
        filtered_qs = base_qs.exclude(
            status=ConversationSession.Status.CLOSED
        ).filter(
            Q(parent_contact__phone_number__icontains=search_q)
            | Q(parent_contact__parent_name__icontains=search_q)
            | Q(active_student__first_name__icontains=search_q)
            | Q(active_student__last_name__icontains=search_q)
            | Q(active_student__admission_number__icontains=search_q)
        )
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
        'page_title': 'WhatsApp',
        'active_tab': active_tab,
        'search_q': search_q,
        'thread_rows': thread_rows,
        'session': session,
        'chat_messages': chat_messages,
        'current_balance': current_balance,
        'status_bot_active': ConversationSession.Status.BOT_ACTIVE,
        'status_escalated': ConversationSession.Status.ESCALATED_PENDING,
        'status_staff_active': ConversationSession.Status.STAFF_ACTIVE,
    }


@method_decorator(school_finance_required, name='dispatch')
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


@method_decorator(school_finance_required, name='dispatch')
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


@method_decorator(school_finance_required, name='dispatch')
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


@method_decorator(school_finance_required, name='dispatch')
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


@method_decorator(school_finance_required, name='dispatch')
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


@method_decorator(school_finance_required, name='dispatch')
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


@method_decorator(school_finance_required, name='dispatch')
class SchoolSettingsView(LoginRequiredMixin, View):
    """School contacts, M-Pesa Paybill credentials, WhatsApp, academic years."""

    template_name = 'dashboard/school_settings.html'

    def _context(self, request, school, *, form=None):
        from finance.services.reminder_service import FeeReminderService
        from tenants.ops import latest_ops_job

        years = list(
            AcademicYear.objects.filter(school=school).order_by('-is_current', '-name')
        )
        reminder_preview = FeeReminderService().preview_overdue_reminders(school)
        last_reminders = latest_ops_job('fee_reminders', school=school)

        def _ops_meta(run, *, stale_hours=36):
            if run is None:
                return None
            age = timezone.now() - run.finished_at
            return {
                'run': run,
                'is_stale': age.total_seconds() > stale_hours * 3600,
                'is_failed': run.status == 'FAILED',
            }

        return {
            'page_title': 'School settings',
            'form': form or SchoolSettingsForm(school=school),
            'academic_years': years,
            'reminder_preview': reminder_preview,
            'ops_reminders': _ops_meta(last_reminders),
        }

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        return render(
            request,
            self.template_name,
            self._context(request, school),
        )

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        action = (request.POST.get('action') or 'settings').strip()

        if action == 'add_year':
            name = (request.POST.get('year_name') or '').strip()
            make_current = request.POST.get('make_current') == 'on'
            if not name:
                messages.error(request, 'Enter an academic year name.')
            elif AcademicYear.objects.filter(school=school, name__iexact=name).exists():
                messages.error(request, 'That academic year already exists.')
            else:
                if make_current:
                    AcademicYear.objects.filter(school=school, is_current=True).update(
                        is_current=False
                    )
                AcademicYear.objects.create(
                    school=school,
                    name=name,
                    is_current=make_current
                    or not AcademicYear.objects.filter(school=school).exists(),
                )
                messages.success(request, f'Academic year “{name}” added.')
            return redirect('dashboard:school_settings')

        if action == 'set_current_year':
            year = get_object_or_404(
                AcademicYear,
                pk=request.POST.get('year_id'),
                school=school,
            )
            AcademicYear.objects.filter(school=school, is_current=True).update(
                is_current=False
            )
            year.is_current = True
            year.save(update_fields=['is_current', 'updated_at'])
            messages.success(request, f'{year.name} is now the current academic year.')
            return redirect('dashboard:school_settings')

        if action == 'register_c2b':
            try:
                ok, payload = register_c2b_urls(school)
            except DarajaError as exc:
                messages.error(request, str(exc))
                return redirect('dashboard:school_settings')
            if ok:
                messages.success(
                    request,
                    'Paybill payments connected. Parents can pay using the student admission number as the account.',
                )
            else:
                desc = (
                    payload.get('ResponseDescription')
                    or payload.get('errorMessage')
                    or payload.get('error')
                    or str(payload)
                )
                messages.error(request, f'Could not connect Paybill payments: {desc}')
            return redirect('dashboard:school_settings')

        if action == 'run_fee_reminders':
            from communications.services.whatsapp_delivery import (
                in_quiet_period,
                next_delivery_at,
            )
            from finance.services.reminder_service import FeeReminderService

            service = FeeReminderService()
            preview = service.preview_overdue_reminders(school)

            if preview['in_quiet_hours']:
                when = next_delivery_at()
                messages.error(
                    request,
                    (
                        'Reminders can only be sent during school messaging hours '
                        '(8:00 am – 8:00 pm Nairobi time). '
                        f'Please try again after {when.strftime("%d %b %Y %H:%M")}.'
                    ),
                )
                return redirect('dashboard:school_settings')

            if preview['eligible'] == 0:
                if preview['at_weekly_limit'] and not preview['already_today']:
                    messages.info(
                        request,
                        (
                            'No reminders were sent. Parents who still owe have '
                            'already received both weekly reminders. We keep messaging '
                            'light so families are not overwhelmed.'
                        ),
                    )
                else:
                    messages.info(
                        request,
                        (
                            'No reminders were sent. Parents who still owe were either '
                            'contacted today, have paused reminders, have an active '
                            'promise, or have already reached this week’s limit.'
                        ),
                    )
                return redirect('dashboard:school_settings')

            confirmed = (request.POST.get('confirm_final_weekly') or '') == '1'
            if preview['would_be_final_weekly'] and not confirmed:
                messages.warning(
                    request,
                    (
                        f'For {preview["would_be_final_weekly"]} parent'
                        f'{"s" if preview["would_be_final_weekly"] != 1 else ""}, '
                        'this would be their second and final reminder this week. '
                        'Tick the confirmation below if you still wish to continue — '
                        'we prefer not to message families more than twice a week.'
                    ),
                )
                return render(
                    request,
                    self.template_name,
                    {
                        **self._context(request, school),
                        'reminder_needs_confirm': True,
                    },
                )

            # Never bypass quiet hours or weekly caps from Settings.
            from tenants.models import OpsJobRun
            from tenants.ops import record_ops_job

            started = timezone.now()
            try:
                result = service.dispatch_overdue_reminders(school, force=False)
                record_ops_job(
                    job_name='fee_reminders',
                    status=OpsJobRun.Status.OK,
                    summary=(
                        f'sent {result.reminded}, scanned {result.scanned}, '
                        f'paused {result.skipped_paused}'
                    ),
                    detail=result.as_console_line(school.name),
                    school=school,
                    started_at=started,
                )
            except Exception as exc:
                record_ops_job(
                    job_name='fee_reminders',
                    status=OpsJobRun.Status.FAILED,
                    summary=str(exc)[:255],
                    detail=str(exc),
                    school=school,
                    started_at=started,
                )
                raise
            parts = [
                f'scanned {result.scanned}',
                f'sent {result.reminded}',
            ]
            if result.skipped_cooldown:
                parts.append(
                    f'{result.skipped_cooldown} skipped (already contacted / weekly limit)'
                )
            if result.skipped_paused:
                parts.append(f'{result.skipped_paused} paused (STOP)')
            if preview['would_be_final_weekly'] and result.reminded:
                parts.append(
                    'some parents received their final reminder for this week'
                )
            messages.success(
                request,
                'Fee reminders finished — ' + ', '.join(parts) + '.',
            )
            return redirect('dashboard:school_settings')

        form = SchoolSettingsForm(request.POST, school=school)
        if form.is_valid():
            form.save()
            from tenants.audit import log_audit_event
            from tenants.models import AuditEvent

            log_audit_event(
                school,
                category=AuditEvent.Category.SETTINGS,
                action='school_settings_saved',
                summary='School settings updated',
                actor=request.user,
                object_type='School',
                object_id=str(school.pk),
            )
            messages.success(request, 'School settings saved.')
            return redirect('dashboard:school_settings')
        return render(
            request,
            self.template_name,
            self._context(request, school, form=form),
            status=400,
        )


@method_decorator(school_admin_required, name='dispatch')
class AuditLogView(LoginRequiredMixin, View):
    """Recent audit events for payments, roles, and settings."""

    template_name = 'dashboard/audit_log.html'

    def get(self, request):
        from tenants.models import AuditEvent

        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        category = (request.GET.get('category') or 'ALL').strip().upper()
        qs = (
            AuditEvent.objects.filter(school=school)
            .select_related('actor')
            .order_by('-created_at')
        )
        if category and category != 'ALL':
            qs = qs.filter(category=category)
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Audit log',
                'events': qs[:200],
                'category': category,
                'category_choices': [
                    ('ALL', 'All'),
                    *AuditEvent.Category.choices,
                ],
            },
        )


@method_decorator(school_admin_required, name='dispatch')
class OnboardingChecklistView(LoginRequiredMixin, View):
    """Simple setup checklist for a new school tenant."""

    template_name = 'dashboard/onboarding.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        has_year = AcademicYear.objects.filter(school=school).exists()
        has_stream = ClassStream.objects.filter(school=school).exists()
        has_students = Student.objects.filter(school=school, is_active=True).exists()
        has_paybill = bool((school.paybill_number or '').strip())
        has_mpesa = bool(
            school.mpesa_consumer_key
            or school.mpesa_consumer_secret
            or school.mpesa_passkey
        )
        has_invoices = FeeInvoice.objects.filter(school=school).exists()
        has_twilio = bool((school.twilio_phone_number or '').strip())
        steps = [
            {
                'title': 'Add academic year',
                'done': has_year,
                'url': reverse('dashboard:school_settings'),
            },
            {
                'title': 'Create classes / streams',
                'done': has_stream,
                'url': reverse('dashboard:classes'),
            },
            {
                'title': 'Import or add students',
                'done': has_students,
                'url': reverse('dashboard:classes'),
            },
            {
                'title': 'Save Paybill number',
                'done': has_paybill,
                'url': reverse('dashboard:school_settings'),
            },
            {
                'title': 'Save Daraja API keys',
                'done': has_mpesa,
                'url': reverse('dashboard:school_settings'),
            },
            {
                'title': 'Generate term fee invoices',
                'done': has_invoices,
                'url': reverse('finance:term_fee_plans'),
            },
            {
                'title': 'Optional: school WhatsApp sender',
                'done': has_twilio,
                'url': reverse('dashboard:school_settings'),
                'optional': True,
            },
        ]
        required = [s for s in steps if not s.get('optional')]
        done_count = sum(1 for s in required if s['done'])
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Get started',
                'steps': steps,
                'done_count': done_count,
                'total_required': len(required),
                'complete': done_count >= len(required),
            },
        )


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
                before = (
                    f'admin={membership.is_admin}, teacher={membership.is_teacher}'
                )
                updated = form.save()
                from tenants.audit import log_audit_event
                from tenants.models import AuditEvent

                log_audit_event(
                    school,
                    category=AuditEvent.Category.ROLE,
                    action='membership_updated',
                    summary=(
                        f'Roles updated for '
                        f'{updated.user.get_full_name() or updated.user.email}'
                    ),
                    actor=request.user,
                    object_type='SchoolMembership',
                    object_id=str(updated.pk),
                    metadata={
                        'before': before,
                        'after': (
                            f'admin={updated.is_admin}, teacher={updated.is_teacher}'
                        ),
                    },
                )
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
class StreamEditView(LoginRequiredMixin, View):
    """Rename a stream from the classes page."""

    def post(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        stream = get_object_or_404(
            ClassStream.objects.select_related('grade_level'),
            school=school,
            slug=stream_slug,
        )
        form = StreamEditForm(
            request.POST,
            instance=stream,
            school=school,
            grade=stream.grade_level,
        )
        if form.is_valid():
            updated = form.save()
            messages.success(request, f'Stream renamed to {updated.name}.')
            return redirect('dashboard:classes')
        messages.error(request, 'Could not rename stream. Check the name.')
        return redirect('dashboard:classes')


@method_decorator(school_admin_required, name='dispatch')
class StreamDeleteView(LoginRequiredMixin, View):
    """Delete a non-default stream with no students."""

    def post(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        stream = get_object_or_404(
            ClassStream.objects.select_related('grade_level'),
            school=school,
            slug=stream_slug,
        )
        if stream.is_default:
            messages.error(
                request,
                'Cannot delete the default Main stream. Rename it instead.',
            )
            return redirect('dashboard:classes')

        student_count = Student.objects.filter(
            school=school,
            current_stream=stream,
        ).count()
        if student_count:
            messages.error(
                request,
                (
                    f'Cannot delete {stream.name}: {student_count} student(s) are still '
                    'assigned. Move them to another stream first.'
                ),
            )
            return redirect('dashboard:classes')

        name = str(stream)
        stream.delete()
        messages.success(request, f'Deleted stream {name}.')
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


class StreamAttendanceView(LoginRequiredMixin, View):
    """Daily attendance marking for a stream (teachers + admins)."""

    template_name = 'dashboard/attendance.html'

    def get(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        attendance_date = _parse_attendance_date(request.GET.get('date'))
        students = list(
            Student.objects.filter(
                school=school,
                current_stream=stream,
                is_active=True,
            ).order_by('admission_number')
        )
        existing = {
            row.student_id: row
            for row in AttendanceRecord.objects.filter(
                school=school,
                stream=stream,
                date=attendance_date,
            )
        }
        rows = []
        for student in students:
            record = existing.get(student.pk)
            rows.append(
                {
                    'student': student,
                    'status': record.status if record else AttendanceRecord.Status.PRESENT,
                    'note': record.note if record else '',
                    'notified': bool(record and record.parent_notified_at),
                }
            )
        counts = {
            'present': sum(
                1 for r in rows if r['status'] == AttendanceRecord.Status.PRESENT
            ),
            'absent': sum(
                1 for r in rows if r['status'] == AttendanceRecord.Status.ABSENT
            ),
            'late': sum(1 for r in rows if r['status'] == AttendanceRecord.Status.LATE),
            'excused': sum(
                1 for r in rows if r['status'] == AttendanceRecord.Status.EXCUSED
            ),
        }
        return render(
            request,
            self.template_name,
            {
                'page_title': f'Attendance · {stream}',
                'stream': stream,
                'attendance_date': attendance_date,
                'rows': rows,
                'counts': counts,
                'status_choices': AttendanceRecord.Status.choices,
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

        attendance_date = _parse_attendance_date(request.POST.get('date'))
        students = list(
            Student.objects.filter(
                school=school,
                current_stream=stream,
                is_active=True,
            )
        )
        valid_statuses = {c[0] for c in AttendanceRecord.Status.choices}
        saved_records = []
        for student in students:
            status = (
                request.POST.get(f'status_{student.pk}') or AttendanceRecord.Status.PRESENT
            ).strip().upper()
            if status not in valid_statuses:
                status = AttendanceRecord.Status.PRESENT
            note = (request.POST.get(f'note_{student.pk}') or '').strip()[:255]
            record, _created = AttendanceRecord.objects.update_or_create(
                school=school,
                student=student,
                date=attendance_date,
                defaults={
                    'stream': stream,
                    'status': status,
                    'note': note,
                    'marked_by': request.user,
                },
            )
            saved_records.append(record)

        messages.success(
            request,
            f'Attendance saved for {attendance_date.strftime("%d %b %Y")} '
            f'({len(saved_records)} students).',
        )

        return redirect(
            f"{reverse('dashboard:stream_attendance', kwargs={'stream_slug': stream.slug})}"
            f'?date={attendance_date.isoformat()}'
        )


class WeeklyParentUpdateView(LoginRequiredMixin, View):
    """Teacher compose/send weekly attendance + optional note to parents."""

    template_name = 'dashboard/weekly_parent_update.html'

    def get(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class.')
            return redirect('dashboard:overview')

        from academics.services.weekly_updates import (
            get_or_create_weekly_update,
            is_weekly_update_window,
            week_end_friday,
        )

        update = get_or_create_weekly_update(school, stream, teacher=request.user)
        lines = list(
            update.feedback_lines.select_related('student').order_by(
                'student__admission_number'
            )
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': f'Weekly update · {stream}',
                'stream': stream,
                'update': update,
                'lines': lines,
                'week_end': week_end_friday(update.week_start),
                'can_send': (
                    is_weekly_update_window()
                    and update.status != update.Status.SENT
                ),
                'in_send_window': is_weekly_update_window(),
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

        from academics.models import WeeklyParentUpdate, WeeklyStudentFeedback
        from academics.services.weekly_updates import (
            get_or_create_weekly_update,
            is_weekly_update_window,
            polish_teacher_note,
            send_weekly_update,
        )

        update = get_or_create_weekly_update(school, stream, teacher=request.user)
        action = (request.POST.get('action') or 'save').strip()

        if update.status == WeeklyParentUpdate.Status.SENT and action != 'save':
            messages.info(request, 'This week’s update was already sent.')
            return redirect(
                'dashboard:weekly_parent_update',
                stream_slug=stream.slug,
            )

        # Persist notes from the form for save / polish / send.
        for line in update.feedback_lines.select_related('student'):
            note = (request.POST.get(f'note_{line.pk}') or '').strip()
            if note != (line.teacher_note or ''):
                line.teacher_note = note
                # Clearing the note resets any previous polish.
                if not note:
                    line.polished_note = ''
                    line.save(update_fields=['teacher_note', 'polished_note'])
                else:
                    line.save(update_fields=['teacher_note'])

        if action == 'polish':
            line = get_object_or_404(
                WeeklyStudentFeedback,
                pk=request.POST.get('line_id'),
                update=update,
                school=school,
            )
            raw = (line.teacher_note or '').strip()
            if not raw:
                messages.warning(request, 'Add a note before clarifying with AI.')
            else:
                polished = polish_teacher_note(
                    raw,
                    student_name=line.student.full_name,
                    school_name=school.name,
                )
                line.polished_note = polished
                line.save(update_fields=['polished_note'])
                messages.success(
                    request,
                    f'Clarified note for {line.student.full_name}. Review before sending.',
                )
            return redirect(
                'dashboard:weekly_parent_update',
                stream_slug=stream.slug,
            )

        if action == 'send':
            if not is_weekly_update_window():
                messages.error(
                    request,
                    'Weekly updates can be sent Friday after 4:00 pm through Sunday '
                    '(Nairobi time).',
                )
                return redirect(
                    'dashboard:weekly_parent_update',
                    stream_slug=stream.slug,
                )
            result = send_weekly_update(update, teacher=request.user)
            if result.get('already_sent'):
                messages.info(request, 'This week’s update was already sent.')
            else:
                messages.success(
                    request,
                    f'Weekly updates sent to {result["sent"]} parent(s); '
                    f'{result["skipped"]} skipped (STOP, missing phone, or send error).',
                )
            return redirect(
                'dashboard:weekly_parent_update',
                stream_slug=stream.slug,
            )

        messages.success(request, 'Draft notes saved.')
        return redirect(
            'dashboard:weekly_parent_update',
            stream_slug=stream.slug,
        )


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
    """Legacy stream charges — redirect to the invoice fee ledger."""

    template_name = 'finance/stream_finance.html'

    def get(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            messages.error(request, 'You do not have access to this class finance.')
            return redirect('dashboard:overview')
        messages.info(
            request,
            'Class fee collections now use the Fee ledger. Record cash on the student statement.',
        )
        return redirect(f"{reverse('finance:fee_ledger')}?stream={stream.pk}")

    def post(self, request, stream_slug):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        stream = _get_accessible_stream(request, school, stream_slug)
        if stream is None:
            return redirect('dashboard:overview')
        messages.error(
            request,
            'Legacy stream payments are disabled. Open the student fee statement to record cash.',
        )
        return redirect(f"{reverse('finance:fee_ledger')}?stream={stream.pk}")


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
