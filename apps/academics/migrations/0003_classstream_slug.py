from django.db import migrations, models
from django.utils.text import slugify


def backfill_stream_slugs(apps, schema_editor):
    ClassStream = apps.get_model('academics', 'ClassStream')
    GradeLevel = apps.get_model('academics', 'GradeLevel')

    used = {}
    for stream in ClassStream.objects.all().iterator():
        grade = GradeLevel.objects.filter(pk=stream.grade_level_id).first()
        grade_name = grade.name if grade else ''
        base = slugify(f'{grade_name}-{stream.name}'.strip('-')) or f'stream-{stream.pk}'
        base = base[:100]
        school_key = str(stream.school_id)
        school_used = used.setdefault(school_key, set())
        candidate = base
        suffix = 2
        while candidate in school_used:
            candidate = f'{base}-{suffix}'
            suffix += 1
        school_used.add(candidate)
        stream.slug = candidate
        stream.save(update_fields=['slug'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('academics', '0002_school_memberships'),
    ]

    operations = [
        migrations.AddField(
            model_name='classstream',
            name='slug',
            field=models.SlugField(
                blank=True,
                default='',
                help_text='URL-safe identifier unique within the school (e.g. grade-6-a).',
                max_length=120,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_stream_slugs, noop_reverse),
        migrations.AlterField(
            model_name='classstream',
            name='slug',
            field=models.SlugField(
                help_text='URL-safe identifier unique within the school (e.g. grade-6-a).',
                max_length=120,
            ),
        ),
        migrations.AddConstraint(
            model_name='classstream',
            constraint=models.UniqueConstraint(
                fields=('school', 'slug'),
                name='academics_unique_stream_slug_per_school',
            ),
        ),
    ]
