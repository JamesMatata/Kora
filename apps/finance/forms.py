from academics.models import ClassStream
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
INPUT_CLASS = (
    'w-full rounded-md border border-zinc-800 bg-black px-3 py-2 '
    'text-zinc-100 outline-none focus:border-yellow-400'
)
CHECKBOX_CLASS = 'h-4 w-4 border border-zinc-800 bg-black accent-yellow-400'


class FeeChargeForm(forms.Form):
    """Create a charge and assign it to students in scope."""

    name = forms.CharField(
        max_length=120,
        label='What is this for?',
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS,
                'placeholder': 'e.g. Sports day, Term 2 tuition',
            }
        ),
    )
    amount = forms.DecimalField(
        min_value=Decimal('0.01'),
        max_digits=12,
        decimal_places=2,
        label='Amount per student',
        widget=forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.01'}),
    )
    due_date = forms.DateField(
        required=False,
        label='Due date',
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    description = forms.CharField(
        required=False,
        max_length=500,
        label='Details',
        widget=forms.Textarea(
            attrs={
                'class': INPUT_CLASS,
                'rows': 3,
                'placeholder': 'Optional notes for parents later',
            }
        ),
    )
    confirmed = forms.BooleanField(required=False, widget=forms.HiddenInput())

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        try:
            return Decimal(amount).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError) as exc:
            raise ValidationError('Enter a valid amount.') from exc

    def clean_name(self):
        return self.cleaned_data['name'].strip()

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('confirmed'):
            raise ValidationError('Please confirm before creating this charge.')
        return cleaned


class SchoolFeeChargeForm(FeeChargeForm):
    SCOPE_SCHOOL = 'school'
    SCOPE_CLASSES = 'classes'

    scope = forms.ChoiceField(
        choices=(
            (SCOPE_SCHOOL, 'Whole school'),
            (SCOPE_CLASSES, 'Selected classes'),
        ),
        initial=SCOPE_SCHOOL,
        widget=forms.RadioSelect,
    )
    streams = forms.ModelMultipleChoiceField(
        queryset=ClassStream.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        if school is not None:
            self.fields['streams'].queryset = (
                ClassStream.objects.filter(school=school)
                .select_related('grade_level')
                .order_by('grade_level__order', 'name')
            )

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get('scope')
        streams = cleaned.get('streams')
        if scope == self.SCOPE_CLASSES and not streams:
            raise ValidationError('Select at least one class.')
        return cleaned
