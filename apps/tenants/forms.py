from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.core.validators import RegexValidator

from tenants.models import School

User = get_user_model()

INPUT_CLASS = (
    'w-full rounded-md border border-zinc-800 bg-black px-3 py-2 '
    'text-zinc-100 outline-none focus:border-yellow-400'
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
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', INPUT_CLASS)


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
