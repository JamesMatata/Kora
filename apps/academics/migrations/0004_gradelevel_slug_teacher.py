from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings
from django.utils.text import slugify


def backfill_grade_slugs(apps, schema_editor):
    GradeLevel = apps.get_model('academics', 'GradeLevel')
    used = {}
    for grade in GradeLevel.objects.all().iterator():
        base = slugify(grade.name) or f'grade-{grade.pk}'
        base = base[:100]
        school_key = str(grade.school_id)
        school_used = used.setdefault(school_key, set())
        candidate = base
        suffix = 2
        while candidate in school_used:
            candidate = f'{base}-{suffix}'
            suffix += 1
        school_used.add(candidate)
        grade.slug = candidate
        grade.save(update_fields=['slug'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('academics', '0003_classstream_slug'),
    ]

    operations = [
        migrations.AddField(
            model_name='gradelevel',
            name='slug',
            field=models.SlugField(
                blank=True,
                default='',
                help_text='URL-safe identifier unique within the school (e.g. grade-6).',
                max_length=120,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='gradelevel',
            name='class_teacher',
            field=models.ForeignKey(
                blank=True,
                help_text='Default class teacher when the grade has no streams yet.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='assigned_grades',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_grade_slugs, noop_reverse),
        migrations.AlterField(
            model_name='gradelevel',
            name='slug',
            field=models.SlugField(
                help_text='URL-safe identifier unique within the school (e.g. grade-6).',
                max_length=120,
            ),
        ),
        migrations.AddConstraint(
            model_name='gradelevel',
            constraint=models.UniqueConstraint(
                fields=('school', 'slug'),
                name='academics_unique_grade_level_slug_per_school',
            ),
        ),
    ]
