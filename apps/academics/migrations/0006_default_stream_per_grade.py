import django.db.models.deletion
from django.db import migrations, models
from django.utils.text import slugify


def forwards_default_streams(apps, schema_editor):
    GradeLevel = apps.get_model('academics', 'GradeLevel')
    ClassStream = apps.get_model('academics', 'ClassStream')
    Student = apps.get_model('academics', 'Student')

    for grade in GradeLevel.objects.all().iterator():
        streams = list(ClassStream.objects.filter(grade_level_id=grade.pk).order_by('name'))
        default = next((s for s in streams if s.is_default), None)
        if default is None and streams:
            default = streams[0]
            default.is_default = True
            default.save(update_fields=['is_default'])
        if default is None:
            base = slugify(f'{grade.name}-Main') or 'main'
            candidate = base[:120]
            suffix = 2
            while ClassStream.objects.filter(school_id=grade.school_id, slug=candidate).exists():
                candidate = f'{base[:100]}-{suffix}'
                suffix += 1
            default = ClassStream.objects.create(
                school_id=grade.school_id,
                grade_level_id=grade.pk,
                name='Main',
                slug=candidate,
                is_default=True,
                class_teacher_id=grade.class_teacher_id,
            )

        Student.objects.filter(
            grade_level_id=grade.pk,
            current_stream__isnull=True,
        ).update(current_stream_id=default.pk)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('academics', '0005_student_grade_placement'),
    ]

    operations = [
        migrations.AddField(
            model_name='classstream',
            name='is_default',
            field=models.BooleanField(
                default=False,
                help_text='Default stream auto-created with every class (usually Main).',
            ),
        ),
        migrations.RunPython(forwards_default_streams, noop_reverse),
        migrations.AddConstraint(
            model_name='classstream',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_default', True)),
                fields=('grade_level',),
                name='academics_unique_default_stream_per_grade',
            ),
        ),
        migrations.AlterField(
            model_name='student',
            name='current_stream',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='students',
                to='academics.classstream',
            ),
        ),
    ]
