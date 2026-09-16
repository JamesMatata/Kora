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
    """Create a term fee plan with school-defined fee line items."""

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

        super().__init__(*args, **kwargs)
        self.school = school
        if school is not None:
            self.fields['grade_level'].queryset = GradeLevel.objects.filter(
                school=school
            ).order_by('order', 'name')
            self.known_categories = list(
                FeeCategory.objects.filter(school=school)
                .order_by('name')
                .values_list('name', flat=True)
            )
        else:
            self.fields['grade_level'].queryset = GradeLevel.objects.none()
            self.known_categories = []
        self.line_rows = self._parse_line_rows_raw()

    def _parse_line_rows_raw(self) -> list[dict[str, str]]:
        """Preserve submitted (or default empty) rows for redisplay."""
        if not self.data:
            return [{'name': '', 'amount': ''}]

        indices: set[int] = set()
        for key in self.data.keys():
            if key.startswith('item_name_'):
                suffix = key[len('item_name_') :]
                if suffix.isdigit():
                    indices.add(int(suffix))
            elif key.startswith('item_amount_'):
                suffix = key[len('item_amount_') :]
                if suffix.isdigit():
                    indices.add(int(suffix))

        if not indices:
            return [{'name': '', 'amount': ''}]

        rows = []
        for index in sorted(indices):
            rows.append(
                {
                    'name': (self.data.get(f'item_name_{index}') or '').strip(),
                    'amount': (self.data.get(f'item_amount_{index}') or '').strip(),
                }
            )
        return rows or [{'name': '', 'amount': ''}]

    def clean(self):
        cleaned = super().clean()
        cleaned['name'] = (cleaned.get('name') or '').strip()
        cleaned['term'] = (cleaned.get('term') or '').strip()

        from finance.models import FeeCategory

        parsed: list[tuple[str, Decimal]] = []
        seen_names: set[str] = set()
        for row in self.line_rows:
            name = (row.get('name') or '').strip()
            raw_amount = (row.get('amount') or '').strip()
            if not name and not raw_amount:
                continue
            if not name:
                raise ValidationError('Each fee item needs a name.')
            if not raw_amount:
                raise ValidationError(f'Enter an amount for “{name}”.')
            try:
                amount = Decimal(raw_amount).quantize(Decimal('0.01'))
            except (InvalidOperation, TypeError) as exc:
                raise ValidationError(f'Invalid amount for “{name}”.') from exc
            if amount <= 0:
                raise ValidationError(f'“{name}” must be greater than 0.')
            key = name.casefold()
            if key in seen_names:
                raise ValidationError(f'Duplicate fee item “{name}”.')
            seen_names.add(key)
            parsed.append((name, amount))

        if not parsed:
            raise ValidationError('Add at least one fee item with a name and amount.')

        if self.school is None:
            raise ValidationError('No school context available.')

        line_items = []
        for name, amount in parsed:
            category = FeeCategory.objects.filter(
                school=self.school,
                name__iexact=name,
            ).first()
            if category is None:
                category = FeeCategory.objects.create(
                    school=self.school,
                    name=name,
                )
            line_items.append((category, amount))
        cleaned['line_items'] = line_items

        if cleaned.get('term'):
            from finance.models import TermFeePlan

            term = cleaned['term']
            grade = cleaned.get('grade_level')
            conflicts = TermFeePlan.objects.filter(
                school=self.school,
                is_active=True,
                term__iexact=term,
            )
            if grade is None:
                # School-wide plan conflicts with any active plan for that term.
                if conflicts.exists():
                    raise ValidationError(
                        'An active fee plan already exists for this term '
                        '(school-wide or for a grade). Deactivate it first, '
                        'or create a plan for a specific grade only after '
                        'removing the school-wide plan.'
                    )
            else:
                # Grade plan conflicts with same-grade or school-wide active plan.
                same_grade = conflicts.filter(grade_level=grade).exists()
                school_wide = conflicts.filter(grade_level__isnull=True).exists()
                if same_grade:
                    raise ValidationError(
                        f'An active fee plan already exists for {grade.name} '
                        f'in “{term}”. Deactivate it before creating another.'
                    )
                if school_wide:
                    raise ValidationError(
                        f'A school-wide active plan already covers “{term}”. '
                        'Deactivate that plan before adding a grade-specific one.'
                    )
        return cleaned
