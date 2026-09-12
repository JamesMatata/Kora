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


class TermFeePlanForm(forms.Form):
    """Create a term fee plan with one or more vote-head amounts."""

    name = forms.CharField(
        max_length=160,
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-black', 'bg-zinc-950'),
                'placeholder': 'e.g. Grade 4 Term 1 fees',
            }
        ),
    )
    term = forms.CharField(
        max_length=64,
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-black', 'bg-zinc-950'),
                'placeholder': 'Term 1 2026',
            }
        ),
    )
    grade_level = forms.ModelChoiceField(
        queryset=None,
        required=False,
        empty_label='All grades',
        widget=forms.Select(attrs={'class': INPUT_CLASS.replace('bg-black', 'bg-zinc-950')}),
    )
    due_date = forms.DateField(
        widget=forms.DateInput(
            attrs={
                'class': INPUT_CLASS.replace('bg-black', 'bg-zinc-950'),
                'type': 'date',
            }
        ),
    )

    def __init__(self, *args, school=None, **kwargs):
        from academics.models import GradeLevel
        from finance.models import FeeCategory
        from finance.services.invoicing import ensure_default_fee_categories

        super().__init__(*args, **kwargs)
        self.school = school
        if school is not None:
            ensure_default_fee_categories(school)
            self.fields['grade_level'].queryset = GradeLevel.objects.filter(
                school=school
            ).order_by('order', 'name')
            self.categories = list(
                FeeCategory.objects.filter(school=school).order_by('name')
            )
        else:
            self.fields['grade_level'].queryset = GradeLevel.objects.none()
            self.categories = []

    def clean(self):
        cleaned = super().clean()
        cleaned['name'] = (cleaned.get('name') or '').strip()
        cleaned['term'] = (cleaned.get('term') or '').strip()

        line_items = []
        for category in self.categories:
            raw = (self.data.get(f'amount_{category.pk}') or '').strip()
            if not raw:
                continue
            try:
                amount = Decimal(raw).quantize(Decimal('0.01'))
            except (InvalidOperation, TypeError) as exc:
                raise ValidationError(
                    f'Invalid amount for {category.name}.'
                ) from exc
            if amount < 0:
                raise ValidationError(f'{category.name} cannot be negative.')
            if amount > 0:
                line_items.append((category, amount))
        if not line_items:
            raise ValidationError('Enter at least one vote-head amount greater than 0.')
        cleaned['line_items'] = line_items
        return cleaned
