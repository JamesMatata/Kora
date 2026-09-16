from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, TemplateView

from tenants.forms import ProfileForm, SchoolApplicationForm, StyledPasswordChangeForm
from tenants.models import (
    Notification,
    PlatformBillingSettings,
    PlatformInvoice,
    School,
    SchoolApplicationDocument,
    SchoolMembership,
    SchoolTermPeriod,
    StaffInvitation,
)
from tenants.platform_billing import (
    approve_school,
    preview_true_up,
    quote_amount,
    record_platform_payment,
    refresh_subscription_state,
    reject_school,
)
from tenants.services import accept_invitation, reject_invitation

User = get_user_model()


class LandingView(View):
    """Public marketing home. Authenticated users go straight into the app."""

    def get(self, request):
        if request.user.is_authenticated:
            return redirect('dashboard:overview')
        return render(request, 'landing.html')


class EmailUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        placeholders = {
            'email': 'Email',
            'first_name': 'First name',
            'last_name': 'Last name',
            'password1': 'Password',
            'password2': 'Confirm password',
        }
        input_class = (
            'w-full rounded-md border border-zinc-800 bg-black px-3 py-2.5 '
            'text-sm text-zinc-100 outline-none placeholder:text-zinc-600 '
            'focus:border-yellow-400'
        )
        password_class = input_class + ' pr-11'
        for name, field in self.fields.items():
            field.label = ''
            field.widget.attrs['placeholder'] = placeholders.get(name, field.label or name)
            field.widget.attrs['class'] = (
                password_class if name.startswith('password') else input_class
            )
            field.widget.attrs['aria-label'] = placeholders.get(name, name)


class TenantSelectView(LoginRequiredMixin, TemplateView):
    template_name = 'tenants/select.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        memberships = getattr(self.request, 'user_memberships', None)
        if memberships is None:
            memberships = list(
                SchoolMembership.objects.filter(
                    user=self.request.user,
                    school__is_active=True,
                )
                .exclude(school__status=School.Status.REJECTED)
                .select_related('school', 'school__subscription')
                .order_by('school__name')
            )
        context['memberships'] = memberships
        context['page_title'] = 'Select school'
        context['pending_invitation_count'] = StaffInvitation.objects.filter(
            email__iexact=self.request.user.email,
            status=StaffInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).count()
        draft = self.request.session.get(TenantCreateView.draft_session_key) or {}
        has_draft = any(
            (draft.get(field) or '').strip()
            for field in TenantCreateView.draft_fields
        )
        context['has_application_draft'] = has_draft
        context['application_draft'] = None
        if has_draft:
            try:
                step = int(draft.get('wizard_step') or 1)
            except (TypeError, ValueError):
                step = 1
            step = max(1, min(3, step))
            step_labels = {
                1: 'School details',
                2: 'Academic terms',
                3: 'Documents',
            }
            context['application_draft'] = {
                'name': (draft.get('name') or '').strip() or 'Untitled school',
                'county': (draft.get('county') or '').strip(),
                'step': step,
                'step_label': step_labels[step],
            }
        return context


class TenantSwitchView(LoginRequiredMixin, View):
    def post(self, request, school_id):
        membership = get_object_or_404(
            SchoolMembership.objects.select_related('school'),
            user=request.user,
            school_id=school_id,
            school__is_active=True,
        )
        school = membership.school
        if school.status == School.Status.REJECTED:
            messages.error(request, 'That school application was declined.')
            return redirect('tenants:select')

        request.session['active_school_id'] = str(membership.school_id)
        if membership.is_admin and membership.is_teacher:
            request.session['active_role_mode'] = 'admin'
        else:
            request.session.pop('active_role_mode', None)
        messages.success(request, f'Switched to {membership.school.name}.')
        if school.status == School.Status.PENDING_REVIEW:
            return redirect('tenants:verification_pending')
        try:
            if school.subscription.is_locked:
                return redirect('tenants:billing')
        except Exception:
            pass
        return redirect('dashboard:overview')

    def get(self, request, school_id):
        return self.post(request, school_id)


class RoleModeSwitchView(LoginRequiredMixin, View):
    """Flip dual-role users between Admin and Teacher focus modes."""

    def post(self, request, mode):
        membership = getattr(request, 'membership', None)
        if membership is None or not (membership.is_admin and membership.is_teacher):
            messages.error(request, 'Role switching is only available for dual-role staff.')
            return redirect('dashboard:overview')

        if mode not in ('admin', 'teacher'):
            messages.error(request, 'Unknown role mode.')
            return redirect('dashboard:overview')

        request.session['active_role_mode'] = mode
        label = 'Admin' if mode == 'admin' else 'Teacher'
        messages.success(request, f'Now working as {label}.')
        return redirect('dashboard:overview')

    def get(self, request, mode):
        return self.post(request, mode)


class TenantCreateView(LoginRequiredMixin, View):
    """Submit a school application for Kora KYB review."""

    template_name = 'tenants/create.html'
    draft_session_key = 'school_application_draft'
    draft_fields = (
        'name',
        'contact_phone',
        'contact_email',
        'physical_address',
        'county',
        'paybill_number',
        'applicant_role',
        'estimated_student_count',
        'academic_year_label',
        'term1_start',
        'term1_end',
        'term2_start',
        'term2_end',
        'term3_start',
        'term3_end',
    )
    step_fields = {
        1: {
            'name',
            'contact_phone',
            'contact_email',
            'physical_address',
            'county',
            'paybill_number',
            'applicant_role',
            'estimated_student_count',
        },
        2: {
            'academic_year_label',
            'term1_start',
            'term1_end',
            'term2_start',
            'term2_end',
            'term3_start',
            'term3_end',
        },
        3: {
            'doc_school_registration',
            'doc_applicant_id',
            'doc_authority_letter',
            'doc_kra_pin',
            'doc_cr12',
        },
    }

    def _draft_from_post(self, request):
        draft = {field: (request.POST.get(field) or '').strip() for field in self.draft_fields}
        try:
            step = int(request.POST.get('wizard_step') or 1)
        except (TypeError, ValueError):
            step = 1
        draft['wizard_step'] = max(1, min(3, step))
        return draft

    def _has_draft_content(self, draft):
        if not draft:
            return False
        return any((draft.get(field) or '').strip() for field in self.draft_fields)

    def _step_for_form_errors(self, form):
        for step, fields in self.step_fields.items():
            if any(name in form.errors for name in fields):
                return step
        if form.non_field_errors():
            return 2
        return 1

    def _render_form(self, request, form, *, status=200, initial_step=1, has_draft=False):
        return render(
            request,
            self.template_name,
            {
                'form': form,
                'page_title': 'Apply to add a school',
                'quote_preview': None,
                'initial_step': initial_step,
                'has_draft': has_draft,
                'cancel_url': reverse_lazy('tenants:select'),
            },
            status=status,
        )

    def get(self, request):
        draft = request.session.get(self.draft_session_key) or {}
        initial = {field: draft.get(field, '') for field in self.draft_fields if draft.get(field)}
        try:
            initial_step = int(draft.get('wizard_step') or 1)
        except (TypeError, ValueError):
            initial_step = 1
        initial_step = max(1, min(3, initial_step))
        form = SchoolApplicationForm(initial=initial) if initial else SchoolApplicationForm()
        return self._render_form(
            request,
            form,
            initial_step=initial_step,
            has_draft=self._has_draft_content(draft),
        )

    def post(self, request):
        intent = (request.POST.get('intent') or 'submit').strip()

        if intent == 'discard':
            request.session.pop(self.draft_session_key, None)
            messages.info(request, 'Application draft discarded.')
            return redirect('tenants:select')

        if intent == 'save_draft':
            draft = self._draft_from_post(request)
            if self._has_draft_content(draft):
                request.session[self.draft_session_key] = draft
                messages.success(
                    request,
                    'Draft saved. Documents are not stored in drafts — re-upload them when you continue.',
                )
            else:
                request.session.pop(self.draft_session_key, None)
                messages.info(request, 'Nothing to save — left without a draft.')
            return redirect('tenants:select')

        form = SchoolApplicationForm(request.POST, request.FILES)
        draft = request.session.get(self.draft_session_key) or {}
        if not form.is_valid():
            return self._render_form(
                request,
                form,
                status=400,
                initial_step=self._step_for_form_errors(form),
                has_draft=self._has_draft_content(draft),
            )

        data = form.cleaned_data
        quote = quote_amount(headcount=data['estimated_student_count'])

        with transaction.atomic():
            school = School.objects.create(
                name=data['name'].strip(),
                code=data['code'],
                contact_phone=data['contact_phone'].strip(),
                contact_email=(data.get('contact_email') or '').strip(),
                paybill_number=(data.get('paybill_number') or '').strip(),
                physical_address=data['physical_address'].strip(),
                county=data['county'].strip(),
                applicant_role=data['applicant_role'],
                estimated_student_count=data['estimated_student_count'],
                status=School.Status.PENDING_REVIEW,
                is_active=True,
                submitted_at=timezone.now(),
            )
            year = data['academic_year_label'].strip()
            for number, start_key, end_key in (
                (1, 'term1_start', 'term1_end'),
                (2, 'term2_start', 'term2_end'),
                (3, 'term3_start', 'term3_end'),
            ):
                SchoolTermPeriod.objects.create(
                    school=school,
                    academic_year_label=year,
                    term_number=number,
                    start_date=data[start_key],
                    end_date=data[end_key],
                )

            doc_map = (
                (
                    SchoolApplicationDocument.DocType.SCHOOL_REGISTRATION,
                    'doc_school_registration',
                ),
                (
                    SchoolApplicationDocument.DocType.APPLICANT_ID,
                    'doc_applicant_id',
                ),
                (
                    SchoolApplicationDocument.DocType.AUTHORITY_LETTER,
                    'doc_authority_letter',
                ),
                (SchoolApplicationDocument.DocType.KRA_PIN, 'doc_kra_pin'),
                (SchoolApplicationDocument.DocType.CR12, 'doc_cr12'),
            )
            for doc_type, field_name in doc_map:
                upload = data.get(field_name)
                if not upload:
                    continue
                SchoolApplicationDocument.objects.create(
                    school=school,
                    doc_type=doc_type,
                    file=upload,
                    original_name=getattr(upload, 'name', '') or '',
                    uploaded_by=request.user,
                )

            membership = SchoolMembership(
                user=request.user,
                school=school,
                is_admin=True,
                is_teacher=False,
            )
            membership.full_clean()
            membership.save()

        request.session.pop(self.draft_session_key, None)
        request.session['active_school_id'] = str(school.id)
        messages.success(
            request,
            (
                f'Application for {school.name} submitted. '
                'Verification usually takes up to 2 business days.'
            ),
        )
        return redirect('tenants:verification_pending')


class VerificationPendingView(LoginRequiredMixin, View):
    template_name = 'tenants/verification_pending.html'

    def get(self, request):
        school = getattr(request, 'school', None)
        if school is None:
            return redirect('tenants:select')
        if school.status == School.Status.APPROVED:
            return redirect('dashboard:overview')
        if school.status == School.Status.REJECTED:
            return render(
                request,
                self.template_name,
                {
                    'page_title': 'Application declined',
                    'school': school,
                    'rejected': True,
                },
            )
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Verification pending',
                'school': school,
                'rejected': False,
            },
        )


class SchoolBillingView(LoginRequiredMixin, View):
    template_name = 'tenants/billing.html'

    def get(self, request):
        school = getattr(request, 'school', None)
        if school is None:
            return redirect('tenants:select')
        if school.status != School.Status.APPROVED:
            return redirect('tenants:verification_pending')

        subscription = getattr(school, 'subscription', None)
        if subscription is not None:
            refresh_subscription_state(school)
            school.refresh_from_db()
            subscription.refresh_from_db()

        invoices = list(
            PlatformInvoice.objects.filter(school=school).order_by('-created_at')[:20]
        )
        cfg = PlatformBillingSettings.load()
        from academics.models import Student

        live_headcount = Student.objects.filter(school=school, is_active=True).count()
        next_term_quote = quote_amount(headcount=live_headcount or 1)

        main = (
            PlatformInvoice.objects.filter(
                school=school,
                kind=PlatformInvoice.Kind.MAIN,
            )
            .exclude(status=PlatformInvoice.Status.VOID)
            .order_by('-created_at')
            .first()
        )
        true_up_preview = None
        if main is not None and main.census_status == PlatformInvoice.CensusStatus.LOCKED:
            if main.true_up_settled_at is None:
                true_up_preview = preview_true_up(
                    main,
                    live_headcount=live_headcount,
                    settings=cfg,
                )

        return render(
            request,
            self.template_name,
            {
                'page_title': 'Kora subscription',
                'school': school,
                'subscription': subscription,
                'invoices': invoices,
                'billing_settings': cfg,
                'live_headcount': live_headcount,
                'next_term_quote': next_term_quote,
                'main_invoice': main,
                'true_up_preview': true_up_preview,
            },
        )


class OpsSchoolReviewListView(LoginRequiredMixin, View):
    template_name = 'tenants/ops_review_list.html'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            messages.error(request, 'Staff access required.')
            return redirect('dashboard:overview')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        pending = (
            School.objects.filter(status=School.Status.PENDING_REVIEW)
            .order_by('submitted_at')
        )
        recent = (
            School.objects.exclude(status=School.Status.PENDING_REVIEW)
            .order_by('-reviewed_at')[:20]
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': 'School applications',
                'pending': pending,
                'recent': recent,
            },
        )


class OpsSchoolReviewDetailView(LoginRequiredMixin, View):
    template_name = 'tenants/ops_review_detail.html'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            messages.error(request, 'Staff access required.')
            return redirect('dashboard:overview')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, school_id):
        school = get_object_or_404(School, pk=school_id)
        docs = school.application_documents.all()
        terms = school.term_periods.all()
        quote = quote_amount(
            headcount=school.estimated_student_count or 1
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': f'Review · {school.name}',
                'school': school,
                'documents': docs,
                'terms': terms,
                'quote': quote,
            },
        )

    def post(self, request, school_id):
        school = get_object_or_404(School, pk=school_id)
        action = (request.POST.get('action') or '').strip()
        if action == 'approve':
            if school.status != School.Status.PENDING_REVIEW:
                messages.error(request, 'Only pending applications can be approved.')
            else:
                approve_school(school, reviewer=request.user)
                messages.success(request, f'{school.name} approved. Trial started.')
            return redirect('tenants:ops_review_detail', school_id=school.id)
        if action == 'reject':
            reason = (request.POST.get('rejection_reason') or '').strip()
            if not reason:
                messages.error(request, 'Provide a rejection reason.')
                return redirect('tenants:ops_review_detail', school_id=school.id)
            reject_school(school, reviewer=request.user, reason=reason)
            messages.success(request, f'{school.name} rejected.')
            return redirect('tenants:ops_review_list')
        if action == 'mark_paid':
            invoice_id = request.POST.get('invoice_id')
            amount = request.POST.get('amount')
            invoice = get_object_or_404(
                PlatformInvoice,
                pk=invoice_id,
                school=school,
            )
            try:
                from decimal import Decimal

                record_platform_payment(invoice, amount=Decimal(amount))
                messages.success(request, 'Payment recorded.')
            except Exception as exc:
                messages.error(request, str(exc))
            return redirect('tenants:ops_review_detail', school_id=school.id)
        messages.error(request, 'Unknown action.')
        return redirect('tenants:ops_review_detail', school_id=school.id)


class ProfileView(LoginRequiredMixin, View):
    template_name = 'tenants/profile.html'

    def _context(self, request, *, profile_form=None, password_form=None, open_password_modal=False):
        return {
            'page_title': 'Profile',
            'profile_form': profile_form or ProfileForm(instance=request.user),
            'password_form': password_form
            or StyledPasswordChangeForm(user=request.user),
            'open_password_modal': open_password_modal,
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        action = request.POST.get('action', 'profile')

        if action == 'password':
            password_form = StyledPasswordChangeForm(
                user=request.user,
                data=request.POST,
            )
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, 'Password updated.')
                return redirect('tenants:profile')
            return render(
                request,
                self.template_name,
                self._context(
                    request,
                    password_form=password_form,
                    open_password_modal=True,
                ),
                status=400,
            )

        profile_form = ProfileForm(request.POST, instance=request.user)
        if profile_form.is_valid():
            profile_form.save()
            messages.success(request, 'Profile updated.')
            return redirect('tenants:profile')
        return render(
            request,
            self.template_name,
            self._context(request, profile_form=profile_form),
            status=400,
        )


class InboxView(LoginRequiredMixin, View):
    template_name = 'tenants/inbox.html'

    def get(self, request):
        invitations = (
            StaffInvitation.objects.filter(
                email__iexact=request.user.email,
                status=StaffInvitation.Status.PENDING,
                expires_at__gt=timezone.now(),
            )
            .select_related('school', 'invited_by')
            .order_by('-created_at')
        )
        notifications = (
            Notification.objects.filter(user=request.user)
            .select_related('school')
            .order_by('-created_at')[:40]
        )
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Inbox',
                'invitations': invitations,
                'notifications': notifications,
            },
        )


class InvitationRespondView(LoginRequiredMixin, View):
    def post(self, request, invitation_id, decision):
        invitation = get_object_or_404(
            StaffInvitation.objects.select_related('school'),
            pk=invitation_id,
            email__iexact=request.user.email,
        )
        try:
            if decision == 'accept':
                membership = accept_invitation(
                    invitation=invitation,
                    user=request.user,
                )
                request.session['active_school_id'] = str(membership.school_id)
                if membership.is_admin and membership.is_teacher:
                    request.session['active_role_mode'] = 'admin'
                else:
                    request.session.pop('active_role_mode', None)
                messages.success(
                    request,
                    f'You joined {membership.school.name}.',
                )
                if membership.school.status == School.Status.PENDING_REVIEW:
                    return redirect('tenants:verification_pending')
                return redirect('dashboard:overview')
            if decision == 'reject':
                reject_invitation(invitation=invitation, user=request.user)
                messages.success(request, 'Invitation declined.')
                return redirect('tenants:inbox')
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('tenants:inbox')

        messages.error(request, 'Unknown action.')
        return redirect('tenants:inbox')


class NotificationMarkReadView(LoginRequiredMixin, View):
    def post(self, request, notification_id):
        notification = get_object_or_404(
            Notification,
            pk=notification_id,
            user=request.user,
        )
        notification.is_read = True
        notification.save(update_fields=['is_read'])
        if notification.link:
            return redirect(notification.link)
        return redirect('tenants:inbox')


class NotificationsMarkAllReadView(LoginRequiredMixin, View):
    def post(self, request):
        Notification.objects.filter(user=request.user, is_read=False).update(
            is_read=True
        )
        messages.success(request, 'All notifications marked as read.')
        return redirect('tenants:inbox')


class SignUpView(CreateView):
    template_name = 'registration/signup.html'
    form_class = EmailUserCreationForm
    success_url = reverse_lazy('tenants:select')

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        pending = StaffInvitation.objects.filter(
            email__iexact=self.object.email,
            status=StaffInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).count()
        if pending:
            messages.success(
                self.request,
                f'Account created. You have {pending} pending invitation'
                f'{"s" if pending != 1 else ""}.',
            )
            return redirect('tenants:inbox')
        messages.success(self.request, 'Account created. Create or select a school.')
        return redirect('tenants:select')
