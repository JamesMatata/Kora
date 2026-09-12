from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.core.validators import RegexValidator

from tenants.models import School

User = get_user_model()

INPUT_CLASS = (
    'w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2 '
    'text-sm text-zinc-100 outline-none focus:border-yellow-400'
)

E164_OR_BLANK = RegexValidator(
    regex=r'^$|^\+[1-9]\d{1,14}$',
    message='Enter a valid E.164 phone number (e.g. +254712345678) or leave blank.',
)


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
    class Meta:
        model = School
        fields = ('name', 'code', 'contact_phone', 'paybill_number')
        labels = {
            'code': 'Domain / slug',
            'paybill_number': 'Paybill / Till',
        }
        widgets = {
            'name': forms.TextInput(
                attrs={
                    'class': (
                        'w-full rounded-md border border-zinc-800 bg-black px-3 py-2 '
                        'text-zinc-100 outline-none focus:border-yellow-400'
                    ),
                    'placeholder': 'St. Austin\'s Academy',
                }
            ),
            'code': forms.TextInput(
                attrs={
                    'class': (
                        'w-full rounded-md border border-zinc-800 bg-black px-3 py-2 '
                        'text-zinc-100 outline-none focus:border-yellow-400'
                    ),
                    'placeholder': 'st-austins',
                }
            ),
            'contact_phone': forms.TextInput(
                attrs={
                    'class': (
                        'w-full rounded-md border border-zinc-800 bg-black px-3 py-2 '
                        'text-zinc-100 outline-none focus:border-yellow-400'
                    ),
                    'placeholder': '+2547...',
                }
            ),
            'paybill_number': forms.TextInput(
                attrs={
                    'class': (
                        'w-full rounded-md border border-zinc-800 bg-black px-3 py-2 '
                        'text-zinc-100 outline-none focus:border-yellow-400'
                    ),
                    'placeholder': '174379',
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['contact_phone'].required = False
        self.fields['contact_phone'].validators.append(E164_OR_BLANK)
        self.fields['paybill_number'].required = False

    def clean_code(self):
        code = self.cleaned_data['code'].strip().lower()
        return code


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
                'class': INPUT_CLASS,
                'placeholder': 'Leave blank to keep existing',
                'autocomplete': 'off',
            },
            render_value=False,
        ),
    )
    mpesa_consumer_secret = forms.CharField(
        required=False,
        label='M-Pesa consumer secret',
        widget=forms.PasswordInput(
            attrs={
                'class': INPUT_CLASS,
                'placeholder': 'Leave blank to keep existing',
                'autocomplete': 'off',
            },
            render_value=False,
        ),
    )
    mpesa_passkey = forms.CharField(
        required=False,
        label='M-Pesa passkey',
        widget=forms.PasswordInput(
            attrs={
                'class': INPUT_CLASS,
                'placeholder': 'Leave blank to keep existing',
                'autocomplete': 'off',
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
        school.set_mpesa_credentials(
            consumer_key=(self.cleaned_data.get('mpesa_consumer_key') or '').strip(),
            consumer_secret=(
                self.cleaned_data.get('mpesa_consumer_secret') or ''
            ).strip(),
            passkey=(self.cleaned_data.get('mpesa_passkey') or '').strip(),
        )
        school.save()
        return school
