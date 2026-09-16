from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.core.validators import RegexValidator
from django.utils.text import slugify

from tenants.models import School

User = get_user_model()

INPUT_CLASS = (
    'w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2 '
    'text-sm text-zinc-100 outline-none focus:border-yellow-400'
)
SECRET_INPUT_CLASS = (
    'w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2 pr-11 '
    'text-sm text-zinc-100 outline-none focus:border-yellow-400'
)

E164_OR_BLANK = RegexValidator(
    regex=r'^$|^\+[1-9]\d{1,14}$',
    message='Enter a valid E.164 phone number (e.g. +254712345678) or leave blank.',
)


def unique_school_code(name: str, *, max_length: int = 64) -> str:
    """Build a unique slug from a school name (never user-entered)."""
    base = slugify(name)[:max_length].strip('-') or 'school'
    code = base
    suffix = 2
    while School.objects.filter(code__iexact=code).exists():
        suffix_str = f'-{suffix}'
        trimmed = base[: max_length - len(suffix_str)].rstrip('-') or 'school'
        code = f'{trimmed}{suffix_str}'
        suffix += 1
    return code


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email')
        widgets = {
            'first_name': forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'given-name'}),
            'last_name': forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'family-name'}),
            'email': forms.EmailInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'email'}),
        }

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        qs = User.objects.filter(email__iexact=email)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('Another account already uses this email.')
        return email


class StyledPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.setdefault('class', INPUT_CLASS)
            field.widget.attrs.setdefault('autocomplete', name)
            field.help_text = ''


class SchoolCreateForm(forms.ModelForm):
    """Deprecated alias — use SchoolApplicationForm."""

    class Meta:
        model = School
        fields = ('name', 'code', 'contact_phone', 'paybill_number')


class SchoolApplicationForm(forms.Form):
    """Apply to add a school (manual KYB review)."""

    name = forms.CharField(
        max_length=255,
        label='School name',
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'placeholder': "St. Austin's Academy",
            }
        ),
    )
    contact_phone = forms.CharField(
        max_length=32,
        validators=[E164_OR_BLANK],
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'placeholder': '+2547…',
            }
        ),
    )
    contact_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black')}
        ),
    )
    physical_address = forms.CharField(
        widget=forms.Textarea(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'rows': 2,
                'placeholder': 'Street, town',
            }
        ),
    )
    county = forms.CharField(
        max_length=64,
        widget=forms.TextInput(
            attrs={'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black')}
        ),
    )
    paybill_number = forms.CharField(
        required=False,
        max_length=32,
        label='Paybill (optional)',
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'placeholder': 'Optional for now',
            }
        ),
    )
    applicant_role = forms.ChoiceField(
        choices=(
            ('Headteacher', 'Headteacher'),
            ('Director', 'Director / Proprietor'),
            ('Bursar', 'Bursar'),
            ('Board member', 'Board member'),
            ('Other', 'Other authorized staff'),
        ),
        widget=forms.Select(
            attrs={'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black')}
        ),
    )
    estimated_student_count = forms.IntegerField(
        min_value=1,
        label='Estimated student headcount',
        widget=forms.NumberInput(
            attrs={'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black')}
        ),
    )
    academic_year_label = forms.CharField(
        max_length=32,
        label='Academic year',
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'placeholder': '2026',
            }
        ),
    )
    term1_start = forms.DateField(
        label='Term 1 start',
        widget=forms.DateInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'type': 'date',
            }
        ),
    )
    term1_end = forms.DateField(
        label='Term 1 end',
        widget=forms.DateInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'type': 'date',
            }
        ),
    )
    term2_start = forms.DateField(
        label='Term 2 start',
        widget=forms.DateInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'type': 'date',
            }
        ),
    )
    term2_end = forms.DateField(
        label='Term 2 end',
        widget=forms.DateInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'type': 'date',
            }
        ),
    )
    term3_start = forms.DateField(
        label='Term 3 start',
        widget=forms.DateInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'type': 'date',
            }
        ),
    )
    term3_end = forms.DateField(
        label='Term 3 end',
        widget=forms.DateInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-zinc-950', 'bg-black'),
                'type': 'date',
            }
        ),
    )
    doc_school_registration = forms.FileField(
        label='School registration / MoE licence',
    )
    doc_applicant_id = forms.FileField(
        label='Applicant National ID / passport',
    )
    doc_authority_letter = forms.FileField(
        label='Authority letter (letterhead / appointment / board resolution)',
    )
    doc_kra_pin = forms.FileField(required=False, label='KRA PIN (optional)')
    doc_cr12 = forms.FileField(required=False, label='CR12 (optional)')

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get('name') or '').strip()
        if name:
            cleaned['code'] = unique_school_code(name)
        pairs = (
            ('term1_start', 'term1_end', 'Term 1'),
            ('term2_start', 'term2_end', 'Term 2'),
            ('term3_start', 'term3_end', 'Term 3'),
        )
        for start_key, end_key, label in pairs:
            start = cleaned.get(start_key)
            end = cleaned.get(end_key)
            if start and end and end < start:
                raise forms.ValidationError(f'{label} end date must be on or after start.')
        return cleaned


class SchoolSettingsForm(forms.Form):
    """Edit school contact + M-Pesa/WhatsApp credentials (secrets optional)."""

    name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    contact_phone = forms.CharField(
        required=False,
        max_length=32,
        validators=[E164_OR_BLANK],
        widget=forms.TextInput(
            attrs={'class': INPUT_CLASS, 'placeholder': '+254712345678'}
        ),
    )
    contact_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={'class': INPUT_CLASS}),
    )
    paybill_number = forms.CharField(
        required=False,
        max_length=32,
        label='Paybill number',
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS,
                'placeholder': 'e.g. 562340',
            }
        ),
        help_text='Your Safaricom Paybill number for parent fee payments.',
    )
    twilio_phone_number = forms.CharField(
        required=False,
        max_length=16,
        label='WhatsApp number',
        validators=[E164_OR_BLANK],
        widget=forms.TextInput(
            attrs={'class': INPUT_CLASS, 'placeholder': '+254712345678'}
        ),
        help_text='Number parents receive school WhatsApp messages from. Leave blank to use the Kora default.',
    )
    mpesa_consumer_key = forms.CharField(
        required=False,
        label='M-Pesa consumer key',
        widget=forms.PasswordInput(
            attrs={
                'class': SECRET_INPUT_CLASS,
                'placeholder': 'Leave blank to keep existing',
                'autocomplete': 'new-password',
                'spellcheck': 'false',
            },
            render_value=False,
        ),
    )
    mpesa_consumer_secret = forms.CharField(
        required=False,
        label='M-Pesa consumer secret',
        widget=forms.PasswordInput(
            attrs={
                'class': SECRET_INPUT_CLASS,
                'placeholder': 'Leave blank to keep existing',
                'autocomplete': 'new-password',
                'spellcheck': 'false',
            },
            render_value=False,
        ),
    )
    mpesa_passkey = forms.CharField(
        required=False,
        label='M-Pesa passkey',
        widget=forms.PasswordInput(
            attrs={
                'class': SECRET_INPUT_CLASS,
                'placeholder': 'Leave blank to keep existing',
                'autocomplete': 'new-password',
                'spellcheck': 'false',
            },
            render_value=False,
        ),
    )
    mpesa_environment = forms.ChoiceField(
        required=False,
        label='M-Pesa mode',
        choices=(
            ('', 'Use Kora default'),
            ('sandbox', 'Testing'),
            ('production', 'Live'),
        ),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='Use Testing while setting up. Switch to Live when Safaricom has approved your Paybill.',
    )
    HOUR_CHOICES = tuple(
        (
            h,
            f'{h % 12 or 12}:00 {"am" if h < 12 else "pm"} ({h:02d}:00)',
        )
        for h in range(24)
    )
    reminder_quiet_hour_start = forms.TypedChoiceField(
        coerce=int,
        choices=HOUR_CHOICES,
        label='Quiet hours start',
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='No automated fee reminders after this hour (Nairobi time).',
    )
    reminder_quiet_hour_end = forms.TypedChoiceField(
        coerce=int,
        choices=HOUR_CHOICES,
        label='Quiet hours end',
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='Reminders may resume from this hour (Nairobi time).',
    )

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        if school is not None and not self.is_bound:
            self.fields['name'].initial = school.name
            self.fields['contact_phone'].initial = school.contact_phone
            self.fields['contact_email'].initial = school.contact_email
            self.fields['paybill_number'].initial = school.paybill_number
            self.fields['twilio_phone_number'].initial = school.twilio_phone_number
            self.fields['mpesa_environment'].initial = school.mpesa_environment or ''
            self.fields['reminder_quiet_hour_start'].initial = (
                school.reminder_quiet_hour_start
            )
            self.fields['reminder_quiet_hour_end'].initial = (
                school.reminder_quiet_hour_end
            )

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('reminder_quiet_hour_start')
        end = cleaned.get('reminder_quiet_hour_end')
        if start is not None and end is not None and start == end:
            raise forms.ValidationError(
                'Quiet hours start and end cannot be the same. '
                'Choose a window (for example 8:00 pm to 8:00 am).'
            )
        return cleaned

    def save(self):
        school = self.school
        school.name = self.cleaned_data['name'].strip()
        school.contact_phone = (self.cleaned_data.get('contact_phone') or '').strip()
        school.contact_email = (self.cleaned_data.get('contact_email') or '').strip()
        school.paybill_number = (self.cleaned_data.get('paybill_number') or '').strip()
        school.twilio_phone_number = (
            self.cleaned_data.get('twilio_phone_number') or ''
        ).strip()
        school.mpesa_environment = (
            self.cleaned_data.get('mpesa_environment') or ''
        ).strip()
        school.reminder_quiet_hour_start = int(
            self.cleaned_data['reminder_quiet_hour_start']
        )
        school.reminder_quiet_hour_end = int(
            self.cleaned_data['reminder_quiet_hour_end']
        )
        school.set_mpesa_credentials(
            consumer_key=(self.cleaned_data.get('mpesa_consumer_key') or '').strip(),
            consumer_secret=(
                self.cleaned_data.get('mpesa_consumer_secret') or ''
            ).strip(),
            passkey=(self.cleaned_data.get('mpesa_passkey') or '').strip(),
        )
        school.save()
        return school


class SchoolTermCalendarForm(forms.Form):
    """Edit the three term date ranges for a school year."""

    academic_year_label = forms.CharField(
        max_length=32,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    term1_start = forms.DateField(
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    term1_end = forms.DateField(
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    term2_start = forms.DateField(
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    term2_end = forms.DateField(
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    term3_start = forms.DateField(
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    term3_end = forms.DateField(
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )

    def clean(self):
        cleaned = super().clean()
        for n in (1, 2, 3):
            start = cleaned.get(f'term{n}_start')
            end = cleaned.get(f'term{n}_end')
            if start and end and end < start:
                raise forms.ValidationError(
                    f'Term {n} end date must be on or after start.'
                )
        return cleaned
