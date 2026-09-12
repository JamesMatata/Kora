from decimal import Decimal
import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from academics.models import ClassStream, GradeLevel, Student
from finance.forms import SchoolFeeChargeForm, TermFeePlanForm
from finance.models import FeeStructure, Payment, TermFeeLineItem, TermFeePlan
from finance.services import create_and_assign_charge, stream_finance_rows
from finance.services.invoicing import generate_invoices_from_plan
from finance.services.reconciliation import (
    ACK,
    C2B_ACCEPT,
    reconcile_c2b_confirmation,
    reconcile_stk_callback,
    validate_c2b_payment,
)
from tenants.decorators import school_admin_required, school_finance_required

logger = logging.getLogger(__name__)


def _ensure_school(request):
    school = getattr(request, 'school', None)
    if school is None:
        messages.error(request, 'No school context available for this account.')
        return None
    return school


@method_decorator(school_finance_required, name='dispatch')
class SchoolFinanceView(LoginRequiredMixin, View):
    """Legacy overview — redirect staff to the invoice fee ledger."""

    template_name = 'finance/school_overview.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        messages.info(
            request,
            'Collections now use Fee ledger and Term fee plans. Legacy charges are retired.',
        )
        return redirect('dashboard:fee_ledger')

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        messages.error(
            request,
            'Legacy one-off charges are disabled. Create a term fee plan instead.',
        )
        return redirect('finance:term_fee_plans')


@method_decorator(school_finance_required, name='dispatch')
class TermFeePlanView(LoginRequiredMixin, View):
    """Create term fee plans (vote-heads) and generate FeeInvoices."""

    template_name = 'finance/term_fee_plans.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        return self._render(request, school)

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        action = (request.POST.get('action') or 'create_plan').strip()
        if action == 'generate':
            plan = get_object_or_404(
                TermFeePlan,
                pk=request.POST.get('plan_id'),
                school=school,
            )
            result = generate_invoices_from_plan(plan, created_by=request.user)
            messages.success(
                request,
                (
                    f'Generated {result["created"]} new invoice(s) for “{plan.name}”. '
                    f'Updated {result.get("updated", 0)} unpaid. '
                    f'Skipped {result["skipped_existing"]} (already paid / unchanged).'
                ),
            )
            return redirect('finance:term_fee_plans')

        form = TermFeePlanForm(request.POST, school=school)
        if not form.is_valid():
            messages.error(request, 'Could not save fee plan. Check the details.')
            return self._render(request, school, form=form, status=400)

        plan = TermFeePlan.objects.create(
            school=school,
            name=form.cleaned_data['name'],
            term=form.cleaned_data['term'],
            grade_level=form.cleaned_data.get('grade_level'),
            due_date=form.cleaned_data['due_date'],
            created_by=request.user,
            is_active=True,
        )
        for category, amount in form.cleaned_data['line_items']:
            TermFeeLineItem.objects.create(
                school=school,
                plan=plan,
                category=category,
                amount=amount,
            )
        messages.success(
            request,
            f'Fee plan “{plan.name}” saved (total {plan.total_amount}). '
            'Generate invoices when ready.',
        )
        return redirect('finance:term_fee_plans')

    def _render(self, request, school, form=None, status=200):
        form = form or TermFeePlanForm(school=school)
        plans = (
            TermFeePlan.objects.filter(school=school)
            .select_related('grade_level', 'created_by')
            .prefetch_related('line_items__category')
            .order_by('-created_at')[:40]
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Fee plans',
                'form': form,
                'plans': plans,
                'categories': getattr(form, 'categories', []),
            },
            status=status,
        )


@csrf_exempt
@require_POST
def daraja_callback(request):
    """
    Public Safaricom Daraja STK callback.

    Query: ?tenant_id=<school UUID>
    Always acknowledges with ResultCode 0 so Safaricom does not retry forever.
    """
    tenant_id = (request.GET.get('tenant_id') or '').strip() or None

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    try:
        result = reconcile_stk_callback(payload=payload, tenant_id=tenant_id)
    except Exception:
        logger.exception(
            'Daraja callback reconciliation failed tenant_id=%s',
            tenant_id,
        )
        result = ACK

    return JsonResponse(result)


@csrf_exempt
@require_POST
def daraja_c2b_validation(request):
    """Safaricom C2B validation webhook (?tenant_id=)."""
    tenant_id = (request.GET.get('tenant_id') or '').strip() or None
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = validate_c2b_payment(payload=payload, tenant_id=tenant_id)
    except Exception:
        logger.exception('C2B validation failed tenant_id=%s', tenant_id)
        result = C2B_ACCEPT
    return JsonResponse(result)


@csrf_exempt
@require_POST
def daraja_c2b_confirmation(request):
    """Safaricom C2B confirmation webhook (?tenant_id=)."""
    tenant_id = (request.GET.get('tenant_id') or '').strip() or None
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = reconcile_c2b_confirmation(payload=payload, tenant_id=tenant_id)
    except Exception:
        logger.exception('C2B confirmation failed tenant_id=%s', tenant_id)
        result = C2B_ACCEPT
    return JsonResponse(result)
