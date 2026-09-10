from django import forms

from academics.models import ClassStream, GradeLevel
from communications.models import BroadcastNotice

INPUT_CLASS = (
    'w-full rounded-md border border-zinc-800 bg-zinc-950 '
    'px-3 py-2 text-sm text-zinc-100 outline-none focus:border-yellow-400'
)


class BroadcastNoticeForm(forms.Form):
    title = forms.CharField(
        max_length=200,
        widget=forms.TextInput(
            attrs={
                'class': INPUT_CLASS,
                'placeholder': 'e.g. Mid-term closing notice',
            }
        ),
    )
    message = forms.CharField(
        widget=forms.Textarea(
            attrs={
                'rows': 5,
                'class': INPUT_CLASS,
                'placeholder': 'Write the announcement parents will receive on WhatsApp…',
            }
        ),
    )
    target_audience = forms.ChoiceField(
        choices=BroadcastNotice.TargetAudience.choices,
        initial=BroadcastNotice.TargetAudience.ALL_PARENTS,
        widget=forms.Select(
            attrs={
                'class': INPUT_CLASS,
                'id': 'broadcast-audience',
            }
        ),
    )
    target_id = forms.IntegerField(
        required=False,
        widget=forms.HiddenInput(attrs={'id': 'id_target_id'}),
    )

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school

    def clean(self):
        cleaned = super().clean()
        audience = cleaned.get('target_audience')
        target_id = cleaned.get('target_id')

        if audience in (
            BroadcastNotice.TargetAudience.GRADE_LEVEL,
            BroadcastNotice.TargetAudience.CLASS_STREAM,
        ):
            if not target_id:
                self.add_error('target_id', 'Select a grade or stream for this audience.')
                return cleaned
            if self.school is None:
                self.add_error(None, 'No school context.')
                return cleaned
            if audience == BroadcastNotice.TargetAudience.GRADE_LEVEL:
                if not GradeLevel.objects.filter(
                    school=self.school,
                    pk=target_id,
                ).exists():
                    self.add_error('target_id', 'Invalid grade for this school.')
            elif not ClassStream.objects.filter(
                school=self.school,
                pk=target_id,
            ).exists():
                self.add_error('target_id', 'Invalid stream for this school.')
        else:
            cleaned['target_id'] = None
        return cleaned
