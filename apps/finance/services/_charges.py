from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch, Q

from academics.models import Student
from finance.models import FeeStructure, StudentFee


def student_fee_totals(student_fees):
    due = Decimal('0.00')
    paid = Decimal('0.00')
    for fee in student_fees:
        if fee.status == StudentFee.Status.WAIVED:
            continue
        due += fee.amount_due
        paid += fee.amount_paid
    return due, paid, due - paid


def _finance_rows_for_students(school, students):
    rows = []
    total_due = Decimal('0.00')
    total_paid = Decimal('0.00')
    for student in students:
        fees = list(student.fees.all())
        due, paid, balance = student_fee_totals(fees)
        total_due += due
        total_paid += paid
        rows.append(
            {
                'student': student,
                'due': due,
                'paid': paid,
                'balance': balance,
                'fees': fees,
            }
        )
    return rows, total_due, total_paid, total_due - total_paid


def _students_with_fees(school, student_qs):
    return student_qs.prefetch_related(
        Prefetch(
            'fees',
            queryset=StudentFee.objects.filter(school=school)
            .select_related('fee_structure')
            .prefetch_related('payments'),
        )
    ).order_by('admission_number')


def stream_finance_rows(school, stream):
    students = _students_with_fees(
        school,
        Student.objects.filter(school=school, current_stream=stream),
    )
    return _finance_rows_for_students(school, students)


def grade_finance_rows(school, grade):
    """Students on a grade with no stream yet."""
    students = _students_with_fees(
        school,
        Student.objects.filter(
            school=school,
            grade_level=grade,
            current_stream__isnull=True,
        ),
    )
    return _finance_rows_for_students(school, students)


@transaction.atomic
def create_and_assign_charge(
    *,
    school,
    name,
    amount,
    due_date=None,
    description='',
    streams=None,
    grades=None,
    created_by=None,
):
    """
    Create a FeeStructure and assign StudentFee rows to active students.

    streams/grades both None → whole school
    streams=[...] → students in those streams
    grades=[...] → unstreamed students in those grades
    Both may be combined.
    """
    stream_list = list(streams) if streams is not None else []
    grade_list = list(grades) if grades is not None else []
    whole_school = streams is None and grades is None

    fee = FeeStructure(
        school=school,
        name=name,
        amount=amount,
        due_date=due_date,
        description=(description or '').strip(),
        created_by=created_by,
        is_active=True,
    )
    if len(stream_list) == 1 and not grade_list:
        only = stream_list[0]
        fee.stream = only
        fee.grade_level = only.grade_level
    elif len(grade_list) == 1 and not stream_list:
        fee.grade_level = grade_list[0]
    fee.full_clean()
    fee.save()

    students = Student.objects.filter(school=school, is_active=True)
    if not whole_school:
        scope = Q(pk__in=[])  # empty
        if stream_list:
            scope |= Q(current_stream_id__in=[s.pk for s in stream_list])
        if grade_list:
            scope |= Q(
                grade_level_id__in=[g.pk for g in grade_list],
                current_stream__isnull=True,
            )
        students = students.filter(scope)

    students = list(students.order_by('admission_number'))
    StudentFee.objects.bulk_create(
        [
            StudentFee(
                school=school,
                student=student,
                fee_structure=fee,
                amount_due=amount,
                status=StudentFee.Status.PENDING,
            )
            for student in students
        ]
    )
    return fee, len(students)
