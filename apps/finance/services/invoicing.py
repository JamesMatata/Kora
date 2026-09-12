"""Generate FeeInvoice rows from term fee plans (vote-head structures)."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from academics.models import Student
from finance.models import FeeInvoice, FeeInvoiceLineItem, TermFeePlan
from finance.services.credits import apply_available_credit_to_invoice


@transaction.atomic
def generate_invoices_from_plan(
    plan: TermFeePlan,
    *,
    created_by=None,
    update_unpaid: bool = True,
) -> dict[str, int]:
    """
    Create or refresh FeeInvoice for each eligible active student.

    - Creates invoices for students missing this term.
    - If update_unpaid=True, refreshes total/due_date/line items when
      paid_amount == 0 (plan edits take effect).
    - Applies any student fee credit to new/open invoices.
    """
    school = plan.school
    total = plan.total_amount
    if total <= 0:
        return {
            'created': 0,
            'updated': 0,
            'skipped_existing': 0,
            'skipped_zero': 0,
        }

    line_defs = list(
        plan.line_items.select_related('category').order_by('id')
    )
    students = Student.objects.filter(school=school, is_active=True)
    if plan.grade_level_id:
        students = students.filter(grade_level_id=plan.grade_level_id)

    created = 0
    updated = 0
    skipped_existing = 0
    for student in students.iterator():
        invoice = (
            FeeInvoice.objects.select_for_update()
            .filter(school=school, student=student, term=plan.term)
            .first()
        )
        if invoice is None:
            invoice = FeeInvoice.objects.create(
                school=school,
                student=student,
                term=plan.term,
                total_amount=total,
                paid_amount=Decimal('0.00'),
                due_date=plan.due_date,
                status=FeeInvoice.Status.PENDING,
            )
            _sync_line_items(invoice, line_defs)
            apply_available_credit_to_invoice(invoice)
            created += 1
            continue

        if (invoice.paid_amount or Decimal('0.00')) > 0:
            skipped_existing += 1
            continue

        if not update_unpaid:
            skipped_existing += 1
            continue

        invoice.total_amount = total
        invoice.due_date = plan.due_date
        invoice.refresh_status(save=False)
        invoice.save(
            update_fields=['total_amount', 'due_date', 'status', 'updated_at']
        )
        _sync_line_items(invoice, line_defs)
        apply_available_credit_to_invoice(invoice)
        updated += 1

    return {
        'created': created,
        'updated': updated,
        'skipped_existing': skipped_existing,
        'skipped_zero': 0,
    }


def _sync_line_items(invoice: FeeInvoice, line_defs) -> None:
    FeeInvoiceLineItem.objects.filter(invoice=invoice).delete()
    for index, line in enumerate(line_defs):
        name = getattr(line.category, 'name', None) or 'Fee'
        FeeInvoiceLineItem.objects.create(
            school_id=invoice.school_id,
            invoice=invoice,
            category_name=name,
            amount=line.amount,
            sort_order=index,
        )


DEFAULT_FEE_CATEGORIES = (
    'Tuition',
    'Lunch',
    'Transport',
    'Activity',
    'Boarding',
)


def ensure_default_fee_categories(school) -> int:
    from finance.models import FeeCategory

    created = 0
    for name in DEFAULT_FEE_CATEGORIES:
        _, was_created = FeeCategory.objects.get_or_create(
            school=school,
            name=name,
            defaults={'description': ''},
        )
        if was_created:
            created += 1
    return created
