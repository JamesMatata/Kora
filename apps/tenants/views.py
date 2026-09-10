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

from tenants.forms import ProfileForm, SchoolCreateForm, StyledPasswordChangeForm
from tenants.models import Notification, SchoolMembership, StaffInvitation
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
                .select_related('school')
                .order_by('school__name')
            )
        context['memberships'] = memberships
        context['page_title'] = 'Select school'
        return context


class TenantSwitchView(LoginRequiredMixin, View):
    def post(self, request, school_id):
        membership = get_object_or_404(
            SchoolMembership.objects.select_related('school'),
            user=request.user,
            school_id=school_id,
            school__is_active=True,
        )
        request.session['active_school_id'] = str(membership.school_id)
        if membership.is_admin and membership.is_teacher:
            request.session['active_role_mode'] = 'admin'
        else:
            request.session.pop('active_role_mode', None)
        messages.success(request, f'Switched to {membership.school.name}.')
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
    template_name = 'tenants/create.html'

    def get(self, request):
        return render(
            request,
            self.template_name,
            {
                'form': SchoolCreateForm(),
                'page_title': 'Create school',
            },
        )

    def post(self, request):
        form = SchoolCreateForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {'form': form, 'page_title': 'Create school'},
                status=400,
            )

        with transaction.atomic():
            school = form.save()
            membership = SchoolMembership(
                user=request.user,
                school=school,
                is_admin=True,
                is_teacher=False,
            )
            membership.full_clean()
            membership.save()

        request.session['active_school_id'] = str(school.id)
        messages.success(request, f'{school.name} is ready. Welcome aboard.')
        return redirect('dashboard:overview')


class ProfileView(LoginRequiredMixin, View):
    template_name = 'tenants/profile.html'

    def get(self, request):
        return render(
            request,
            self.template_name,
            {
                'page_title': 'Profile',
                'profile_form': ProfileForm(instance=request.user),
                'password_form': StyledPasswordChangeForm(user=request.user),
            },
        )

    def post(self, request):
        action = request.POST.get('action', 'profile')
        profile_form = ProfileForm(instance=request.user)
        password_form = StyledPasswordChangeForm(user=request.user)

        if action == 'password':
            password_form = StyledPasswordChangeForm(user=request.user, data=request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, 'Password updated.')
                return redirect('tenants:profile')
        else:
            profile_form = ProfileForm(request.POST, instance=request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, 'Profile updated.')
                return redirect('tenants:profile')

        return render(
            request,
            self.template_name,
            {
                'page_title': 'Profile',
                'profile_form': profile_form,
                'password_form': password_form,
            },
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
