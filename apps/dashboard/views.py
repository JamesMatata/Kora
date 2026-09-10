import csv

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView

from academics.models import ClassStream, GradeLevel, Student
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
from dashboard.services.student_import import (
    TEMPLATE_HEADERS,
    import_students_for_stream,
    parse_student_spreadsheet,
)
from finance.forms import FeeChargeForm
from finance.models import Payment, StudentFee
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


class OverviewView(LoginRequiredMixin, TemplateView):
    template_name = 'dashboard/overview.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        school = request.school
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
        context.update(
            {
                'staff_count': staff.count(),
                'student_count': students.count(),
                'stream_count': streams.count(),
                'admin_count': _active_admin_count(school),
            }
        )
        return context


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
