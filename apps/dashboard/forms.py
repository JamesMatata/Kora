from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from academics.models import ClassStream, GradeLevel, Student
from tenants.models import SchoolMembership, StaffInvitation

User = get_user_model()

INPUT_CLASS = (
    'w-full rounded-md border border-zinc-800 bg-black px-3 py-2 '
    'text-zinc-100 outline-none focus:border-yellow-400'
)
CHECKBOX_CLASS = 'h-4 w-4 border border-zinc-800 bg-black accent-yellow-400'


class StaffInviteForm(forms.ModelForm):
    class Meta:
        model = StaffInvitation
        fields = ('email', 'role_admin', 'role_teacher')
        widgets = {
            'email': forms.EmailInput(
                attrs={
                    'class': INPUT_CLASS,
                    'placeholder': 'teacher@school.ac.ke',
                    'autocomplete': 'email',
                }
            ),
            'role_admin': forms.CheckboxInput(attrs={'class': CHECKBOX_CLASS}),
            'role_teacher': forms.CheckboxInput(attrs={'class': CHECKBOX_CLASS}),
        }

    def __init__(self, *args, school=None, admin_slots_remaining=0, **kwargs):
        self.school = school
        self.admin_slots_remaining = admin_slots_remaining
        super().__init__(*args, **kwargs)
        if admin_slots_remaining <= 0:
            self.fields['role_admin'].disabled = True
            self.fields['role_admin'].help_text = 'School already has 2 active admins.'

    def clean(self):
        cleaned = super().clean()
        role_admin = cleaned.get('role_admin')
        role_teacher = cleaned.get('role_teacher')

        if self.fields['role_admin'].disabled:
            role_admin = False
            cleaned['role_admin'] = False

        if not role_admin and not role_teacher:
            raise ValidationError('Select at least one role: Admin or Teacher.')

        if role_admin and self.admin_slots_remaining <= 0:
            raise ValidationError(
                {'role_admin': 'A school may have at most 2 active school admins.'}
            )

        return cleaned

    def save(self, commit=True, invited_by=None):
        invitation = super().save(commit=False)
        invitation.school = self.school
        invitation.expires_at = timezone.now() + timedelta(days=7)
        invitation.status = StaffInvitation.Status.PENDING
        if invited_by is not None:
            invitation.invited_by = invited_by
        if commit:
            invitation.full_clean()
            invitation.save()
        return invitation


class StaffMemberEditForm(forms.Form):
    first_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    email = forms.EmailField(widget=forms.EmailInput(attrs={'class': INPUT_CLASS}))
    is_admin = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': CHECKBOX_CLASS}),
    )
    is_teacher = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': CHECKBOX_CLASS}),
    )
    is_active = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': CHECKBOX_CLASS}),
    )

    def __init__(
        self,
        *args,
        school=None,
        membership=None,
        admin_slots_remaining=0,
        **kwargs,
    ):
        self.school = school
        self.membership = membership
        self.admin_slots_remaining = admin_slots_remaining
        super().__init__(*args, **kwargs)
        if membership is not None and not args:
            user = membership.user
            self.fields['first_name'].initial = user.first_name
            self.fields['last_name'].initial = user.last_name
            self.fields['email'].initial = user.email
            self.fields['is_admin'].initial = membership.is_admin
            self.fields['is_teacher'].initial = membership.is_teacher
            self.fields['is_active'].initial = user.is_active

        # If this membership already has admin, they keep the slot when editing.
        if (
            membership is not None
            and membership.is_admin
            and membership.user.is_active
        ):
            pass
        elif admin_slots_remaining <= 0:
            if not (membership and membership.is_admin):
                self.fields['is_admin'].disabled = True

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if not email:
            raise ValidationError('Email is required.')
        qs = User.objects.filter(email__iexact=email)
        if self.membership is not None:
            qs = qs.exclude(pk=self.membership.user_id)
        if qs.exists():
            raise ValidationError('Another account already uses this email.')
        return email

    def clean(self):
        cleaned = super().clean()
        is_admin = cleaned.get('is_admin')
        is_teacher = cleaned.get('is_teacher')
        is_active = cleaned.get('is_active')

        if self.fields['is_admin'].disabled:
            is_admin = bool(self.membership and self.membership.is_admin)
            cleaned['is_admin'] = is_admin

        if not is_admin and not is_teacher:
            raise ValidationError('Select at least one role: Admin or Teacher.')

        was_active_admin = bool(
            self.membership
            and self.membership.is_admin
            and self.membership.user.is_active
        )
        will_be_active_admin = bool(is_admin and is_active)
        if will_be_active_admin and not was_active_admin:
            if self.admin_slots_remaining <= 0:
                raise ValidationError(
                    {'is_admin': 'A school may have at most 2 active school admins.'}
                )

        if was_active_admin and not will_be_active_admin:
            other_admins = SchoolMembership.objects.filter(
                school=self.school,
                is_admin=True,
                user__is_active=True,
            )
            if self.membership is not None:
                other_admins = other_admins.exclude(pk=self.membership.pk)
            if not other_admins.exists():
                raise ValidationError(
                    'Cannot remove or deactivate the last active admin for this school.'
                )

        return cleaned

    def save(self):
        membership = self.membership
        user = membership.user
        user.first_name = self.cleaned_data.get('first_name') or ''
        user.last_name = self.cleaned_data.get('last_name') or ''
        user.email = self.cleaned_data['email']
        user.is_active = bool(self.cleaned_data.get('is_active'))
        user.save()

        membership.is_admin = bool(self.cleaned_data.get('is_admin'))
        membership.is_teacher = bool(self.cleaned_data.get('is_teacher'))
        membership.full_clean()
        membership.save()
        return membership


class ClassTeacherAssignForm(forms.ModelForm):
    class Meta:
        model = ClassStream
        fields = ('class_teacher',)
        widgets = {
            'class_teacher': forms.Select(
                attrs={
                    'class': (
                        'w-full rounded-md border border-zinc-800 bg-black px-2 py-1.5 '
                        'text-sm text-zinc-100 outline-none focus:border-yellow-400'
                    ),
                }
            ),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        teacher_ids = SchoolMembership.objects.filter(
            school=school,
            is_teacher=True,
            user__is_active=True,
        ).values_list('user_id', flat=True)
        teachers = User.objects.filter(id__in=teacher_ids).order_by(
            'first_name',
            'last_name',
            'email',
        )
        self.fields['class_teacher'].queryset = teachers
        self.fields['class_teacher'].required = False
        self.fields['class_teacher'].empty_label = 'Unassigned'


class GradeTeacherAssignForm(forms.ModelForm):
    class Meta:
        model = GradeLevel
        fields = ('class_teacher',)
        widgets = {
            'class_teacher': forms.Select(
                attrs={
                    'class': (
                        'w-full rounded-md border border-zinc-800 bg-black px-2 py-1.5 '
                        'text-sm text-zinc-100 outline-none focus:border-yellow-400'
                    ),
                }
            ),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        teacher_ids = SchoolMembership.objects.filter(
            school=school,
            is_teacher=True,
            user__is_active=True,
        ).values_list('user_id', flat=True)
        teachers = User.objects.filter(id__in=teacher_ids).order_by(
            'first_name',
            'last_name',
            'email',
        )
        self.fields['class_teacher'].queryset = teachers
        self.fields['class_teacher'].required = False
        self.fields['class_teacher'].empty_label = 'Unassigned'


class GradeLevelForm(forms.ModelForm):
    class Meta:
        model = GradeLevel
        fields = ('name', 'order')
        widgets = {
            'name': forms.TextInput(
                attrs={'class': INPUT_CLASS, 'placeholder': 'Grade 6'}
            ),
            'order': forms.NumberInput(attrs={'class': INPUT_CLASS, 'min': 0}),
        }

    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise ValidationError('Name is required.')
        qs = GradeLevel.objects.filter(school=self.school, name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError('A class with this name already exists.')
        return name

    def save(self, commit=True):
        grade = super().save(commit=False)
        grade.school = self.school
        if not grade.slug:
            grade.ensure_unique_slug()
        if commit:
            grade.full_clean()
            grade.save()
        return grade


class StreamCreateForm(forms.ModelForm):
    class Meta:
        model = ClassStream
        fields = ('name',)
        widgets = {
            'name': forms.TextInput(
                attrs={
                    'class': INPUT_CLASS,
                    'placeholder': 'Stream name',
                }
            ),
        }

    def __init__(self, *args, school=None, grade=None, **kwargs):
        self.school = school
        self.grade = grade
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise ValidationError('Stream name is required.')
        qs = ClassStream.objects.filter(
            school=self.school,
            grade_level=self.grade,
            name__iexact=name,
        )
        if qs.exists():
            raise ValidationError('That stream already exists in this class.')
        return name

    def save(self, commit=True):
        stream = super().save(commit=False)
        stream.school = self.school
        stream.grade_level = self.grade
        if self.grade and self.grade.class_teacher_id and not stream.class_teacher_id:
            stream.class_teacher_id = self.grade.class_teacher_id
        if commit:
            stream.full_clean()
            stream.save()
        return stream


class StudentForm(forms.ModelForm):
    class Meta:
        model = Student
        fields = (
            'admission_number',
            'first_name',
            'last_name',
            'parent_name',
            'parent_phone',
            'current_stream',
            'is_active',
        )
        widgets = {
            'admission_number': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'first_name': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'last_name': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'parent_name': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'parent_phone': forms.TextInput(
                attrs={
                    'class': INPUT_CLASS,
                    'placeholder': '+2547...',
                }
            ),
            'current_stream': forms.Select(attrs={'class': INPUT_CLASS}),
            'is_active': forms.CheckboxInput(attrs={'class': CHECKBOX_CLASS}),
        }

    def __init__(
        self,
        *args,
        school=None,
        stream_queryset=None,
        fixed_stream=None,
        fixed_grade=None,
        **kwargs,
    ):
        self.school = school
        self.fixed_stream = fixed_stream
        self.fixed_grade = fixed_grade
        super().__init__(*args, **kwargs)
        streams = stream_queryset
        if streams is None:
            streams = ClassStream.objects.filter(school=school).select_related(
                'grade_level'
            )
        if 'current_stream' in self.fields:
            self.fields['current_stream'].queryset = streams.order_by(
                'grade_level__order',
                'name',
            )
            self.fields['current_stream'].required = False
        if fixed_stream is not None or fixed_grade is not None:
            self.fields.pop('current_stream', None)
        self.fields['parent_phone'].help_text = 'E.164 format, e.g. +254712345678'

    def clean_admission_number(self):
        value = (self.cleaned_data.get('admission_number') or '').strip()
        if not value:
            raise ValidationError('Admission number is required.')
        qs = Student.objects.filter(school=self.school, admission_number__iexact=value)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError('A student with this admission number already exists.')
        return value

    def clean_current_stream(self):
        stream = self.cleaned_data.get('current_stream')
        if stream and self.school and stream.school_id != self.school.pk:
            raise ValidationError('Stream must belong to this school.')
        return stream

    def save(self, commit=True):
        student = super().save(commit=False)
        student.school = self.school
        if self.fixed_stream is not None:
            student.current_stream = self.fixed_stream
            student.grade_level = self.fixed_stream.grade_level
        elif self.fixed_grade is not None:
            student.grade_level = self.fixed_grade
            student.current_stream = self.fixed_grade.ensure_default_stream()
        elif student.current_stream_id:
            student.grade_level = student.current_stream.grade_level
        if commit:
            student.full_clean()
            student.save()
        return student


class StudentImportUploadForm(forms.Form):
    file = forms.FileField(
        label='Spreadsheet',
        help_text='Upload a .csv or .xlsx file using the template columns.',
        widget=forms.FileInput(
            attrs={
                'class': INPUT_CLASS,
                'accept': (
                    '.csv,.xlsx,'
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,'
                    'text/csv'
                ),
            }
        ),
    )

    def clean_file(self):
        uploaded = self.cleaned_data['file']
        name = (uploaded.name or '').lower()
        if not (name.endswith('.csv') or name.endswith('.xlsx')):
            raise ValidationError('Only .csv or .xlsx files are supported.')
        if uploaded.size > 5 * 1024 * 1024:
            raise ValidationError('File is too large (max 5 MB).')
        return uploaded


class ManualPaymentForm(forms.Form):
    amount = forms.DecimalField(
        min_value=Decimal('0.01'),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.01'}),
    )
    note = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={'class': INPUT_CLASS, 'placeholder': 'Optional note'}
        ),
    )

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        try:
            return Decimal(amount).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError) as exc:
            raise ValidationError('Enter a valid amount.') from exc
