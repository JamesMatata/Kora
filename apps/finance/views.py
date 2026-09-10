from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from academics.models import ClassStream, Student
from finance.forms import SchoolFeeChargeForm
from finance.models import FeeStructure, Payment
from finance.services import create_and_assign_charge, stream_finance_rows
from tenants.decorators import school_admin_required


def _ensure_school(request):
    school = getattr(request, 'school', None)
    if school is None:
        messages.error(request, 'No school context available for this account.')
        return None
    return school


@method_decorator(school_admin_required, name='dispatch')
class SchoolFinanceView(LoginRequiredMixin, View):
    template_name = 'finance/school_overview.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        return self._render(request, school, SchoolFeeChargeForm(school=school))

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        form = SchoolFeeChargeForm(request.POST, school=school)
        if not form.is_valid():
            messages.error(request, 'Could not create charge. Check the details.')
            return self._render(
                request,
                school,
                form,
                show_charge_modal=True,
                status=400,
            )

        scope = form.cleaned_data['scope']
        streams = None
        if scope == SchoolFeeChargeForm.SCOPE_CLASSES:
            streams = list(form.cleaned_data['streams'])

        fee, assigned = create_and_assign_charge(
            school=school,
            name=form.cleaned_data['name'],
            amount=form.cleaned_data['amount'],
            due_date=form.cleaned_data.get('due_date'),
            description=form.cleaned_data.get('description') or '',
            streams=streams,
            created_by=request.user,
        )
        if assigned == 0:
            messages.warning(
                request,
                f'Charge “{fee.name}” was created but no active students matched.',
            )
        else:
            messages.success(
                request,
                f'Charge “{fee.name}” assigned to {assigned} student'
                f'{"s" if assigned != 1 else ""}.',
            )
        return redirect('finance:overview')

    def _render(self, request, school, charge_form, show_charge_modal=False, status=200):
        streams = (
            ClassStream.objects.filter(school=school)
            .select_related('grade_level')
            .order_by('grade_level__order', 'name')
        )
        stream_summaries = []
        school_due = Decimal('0.00')
        school_paid = Decimal('0.00')
        for stream in streams:
            _rows, due, paid, balance = stream_finance_rows(school, stream)
            school_due += due
            school_paid += paid
            stream_summaries.append(
                {
                    'stream': stream,
                    'due': due,
                    'paid': paid,
                    'balance': balance,
                    'student_count': Student.objects.filter(
                        school=school,
                        current_stream=stream,
                        is_active=True,
                    ).count(),
                }
            )

        recent_payments = (
            Payment.objects.filter(school=school)
            .select_related(
                'student_fee__student',
                'student_fee__fee_structure',
                'recorded_by',
            )
            .order_by('-paid_at')[:15]
        )
        recent_charges = (
            FeeStructure.objects.filter(school=school)
            .select_related('stream', 'grade_level', 'created_by')
            .order_by('-created_at')[:10]
        )

        return render(
            request,
            self.template_name,
            {
                'page_title': 'Finance',
                'stream_summaries': stream_summaries,
                'school_due': school_due,
                'school_paid': school_paid,
                'school_balance': school_due - school_paid,
                'recent_payments': recent_payments,
                'recent_charges': recent_charges,
                'charge_form': charge_form,
                'show_charge_modal': show_charge_modal,
                'total_active_students': Student.objects.filter(
                    school=school,
                    is_active=True,
                ).count(),
            },
            status=status,
        )
