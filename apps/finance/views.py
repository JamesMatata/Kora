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

from finance.forms import TermFeePlanForm
from finance.models import TermFeeLineItem, TermFeePlan
from finance.services.invoicing import generate_invoices_from_plan
from finance.services.reconciliation import (
    ACK,
    C2B_ACCEPT,
    reconcile_c2b_confirmation,
    reconcile_stk_callback,
    validate_c2b_payment,
)
from tenants.decorators import school_finance_required

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
            'Use Finance and Fee plans. Legacy charges are retired.',
        )
        return redirect('finance:fee_ledger')

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
    """List term fee plans (create is a separate page)."""

    template_name = 'finance/term_fee_plans.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        plans = (
            TermFeePlan.objects.filter(school=school)
            .select_related('grade_level', 'created_by')
            .prefetch_related('line_items__category')
            .order_by('-is_active', '-created_at')[:80]
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Fee plans',
                'plans': plans,
            },
        )

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')

        action = (request.POST.get('action') or '').strip()
        plan = get_object_or_404(
            TermFeePlan,
            pk=request.POST.get('plan_id'),
            school=school,
        )

        if action == 'generate':
            if not plan.is_active:
                messages.error(request, 'Activate the plan before generating invoices.')
                return redirect('finance:term_fee_plans')
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

        if action == 'deactivate':
            plan.is_active = False
            plan.save(update_fields=['is_active', 'updated_at'])
            messages.success(request, f'“{plan.name}” deactivated.')
            return redirect('finance:term_fee_plans')

        if action == 'activate':
            # Re-run conflict checks before reactivating.
            conflicts = TermFeePlan.objects.filter(
                school=school,
                is_active=True,
                term__iexact=plan.term,
            ).exclude(pk=plan.pk)
            if plan.grade_level_id is None:
                if conflicts.exists():
                    messages.error(
                        request,
                        'Cannot activate: another active plan already covers this term.',
                    )
                    return redirect('finance:term_fee_plans')
            else:
                if conflicts.filter(grade_level=plan.grade_level).exists():
                    messages.error(
                        request,
                        'Cannot activate: an active plan already exists for this grade and term.',
                    )
                    return redirect('finance:term_fee_plans')
                if conflicts.filter(grade_level__isnull=True).exists():
                    messages.error(
                        request,
                        'Cannot activate: a school-wide plan already covers this term.',
                    )
                    return redirect('finance:term_fee_plans')
            plan.is_active = True
            plan.save(update_fields=['is_active', 'updated_at'])
            messages.success(request, f'“{plan.name}” activated.')
            return redirect('finance:term_fee_plans')

        messages.error(request, 'Unknown action.')
        return redirect('finance:term_fee_plans')


@method_decorator(school_finance_required, name='dispatch')
class TermFeePlanCreateView(LoginRequiredMixin, View):
    """Create a new term fee plan with conflict checks."""

    template_name = 'finance/term_fee_plan_form.html'

    def get(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        form = TermFeePlanForm(school=school)
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Add fee plan',
                'form': form,
                'line_rows': form.line_rows,
                'known_categories': form.known_categories,
            },
        )

    def post(self, request):
        school = _ensure_school(request)
        if school is None:
            return redirect('tenants:select')
        form = TermFeePlanForm(request.POST, school=school)
        if not form.is_valid():
            messages.error(request, 'Could not save fee plan. Check the details.')
            return render(
                request,
                self.template_name,
                {
                    'page_title': 'Add fee plan',
                    'form': form,
                    'line_rows': form.line_rows,
                    'known_categories': form.known_categories,
                },
                status=400,
            )

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
            'Generate invoices from the plans list when ready.',
        )
        return redirect('finance:term_fee_plans')


@csrf_exempt
@require_POST
def daraja_callback(request):
    """
    Public Safaricom Daraja STK callback.

    Query: ?tenant_id=<school UUID>&token=<webhook secret>
    Always acknowledges with ResultCode 0 so Safaricom does not retry forever.
    """
    from finance.services.daraja_webhook import authenticate_daraja_callback

    school = authenticate_daraja_callback(request)
    if school is None:
        return JsonResponse(ACK)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    try:
        result = reconcile_stk_callback(
            payload=payload,
            tenant_id=str(school.id),
        )
    except Exception:
        logger.exception(
            'Daraja callback reconciliation failed tenant_id=%s',
            school.id,
        )
        result = ACK

    return JsonResponse(result)


@csrf_exempt
@require_POST
def daraja_c2b_validation(request):
    """Safaricom C2B validation webhook (?tenant_id=&token=)."""
    from finance.services.daraja_webhook import authenticate_daraja_callback
    from finance.services.reconciliation import C2B_REJECT

    school = authenticate_daraja_callback(request)
    if school is None:
        return JsonResponse(C2B_REJECT)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = validate_c2b_payment(
            payload=payload,
            tenant_id=str(school.id),
        )
    except Exception:
        logger.exception('C2B validation failed tenant_id=%s', school.id)
        result = C2B_ACCEPT
    return JsonResponse(result)


@csrf_exempt
@require_POST
def daraja_c2b_confirmation(request):
    """Safaricom C2B confirmation webhook (?tenant_id=&token=)."""
    from finance.services.daraja_webhook import authenticate_daraja_callback

    school = authenticate_daraja_callback(request)
    if school is None:
        return JsonResponse(C2B_ACCEPT)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = reconcile_c2b_confirmation(
            payload=payload,
            tenant_id=str(school.id),
        )
    except Exception:
        logger.exception('C2B confirmation failed tenant_id=%s', school.id)
        result = C2B_ACCEPT
    return JsonResponse(result)
