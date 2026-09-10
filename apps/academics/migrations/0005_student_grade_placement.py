import django.db.models.deletion
from django.db import migrations, models


def forwards_backfill_grade(apps, schema_editor):
    Student = apps.get_model('academics', 'Student')
    for student in Student.objects.select_related('current_stream').iterator():
        if student.current_stream_id and not student.grade_level_id:
            student.grade_level_id = student.current_stream.grade_level_id
            student.save(update_fields=['grade_level_id'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('academics', '0004_gradelevel_slug_teacher'),
    ]

    operations = [
        migrations.AddField(
            model_name='student',
            name='grade_level',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='students',
                to='academics.gradelevel',
            ),
        ),
        migrations.AlterField(
            model_name='student',
            name='current_stream',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='students',
                to='academics.classstream',
            ),
        ),
        migrations.RunPython(forwards_backfill_grade, noop_reverse),
        migrations.AlterField(
            model_name='student',
            name='grade_level',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='students',
                to='academics.gradelevel',
            ),
        ),
    ]
