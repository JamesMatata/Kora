from django.urls import path

from dashboard.views import (
    AssignClassTeacherView,
    AssignGradeTeacherView,
    ClassStreamAssignmentView,
    GradeCreateView,
    GradeEditView,
    GradeFinanceView,
    GradeHomeView,
    GradeStudentCreateView,
    GradeStudentEditView,
    GradeStudentImportTemplateView,
    GradeStudentImportView,
    OverviewView,
    StreamCreateView,
    StreamFinanceView,
    StreamRosterView,
    StudentCreateView,
    StudentEditView,
    StudentImportTemplateView,
    StudentImportView,
    TeacherManagementView,
)

app_name = 'dashboard'

urlpatterns = [
    path('app/', OverviewView.as_view(), name='overview'),
    path('teachers/', TeacherManagementView.as_view(), name='teachers'),
    path('classes/', ClassStreamAssignmentView.as_view(), name='classes'),
    path('classes/new/', GradeCreateView.as_view(), name='grade_create'),
    path(
        'classes/<slug:grade_slug>/edit/',
        GradeEditView.as_view(),
        name='grade_edit',
    ),
    path(
        'classes/<slug:grade_slug>/assign-teacher/',
        AssignGradeTeacherView.as_view(),
        name='assign_grade_teacher',
    ),
    path(
        'classes/<slug:grade_slug>/streams/new/',
        StreamCreateView.as_view(),
        name='stream_create',
    ),
    path(
        'classes/streams/<slug:stream_slug>/assign-teacher/',
        AssignClassTeacherView.as_view(),
        name='assign_teacher',
    ),
    path(
        'grades/<slug:grade_slug>/',
        GradeHomeView.as_view(),
        name='grade_home',
    ),
    path(
        'grades/<slug:grade_slug>/finance/',
        GradeFinanceView.as_view(),
        name='grade_finance',
    ),
    path(
        'grades/<slug:grade_slug>/students/new/',
        GradeStudentCreateView.as_view(),
        name='grade_student_create',
    ),
    path(
        'grades/<slug:grade_slug>/students/<str:admission_number>/edit/',
        GradeStudentEditView.as_view(),
        name='grade_student_edit',
    ),
    path(
        'grades/<slug:grade_slug>/import/',
        GradeStudentImportView.as_view(),
        name='grade_student_import',
    ),
    path(
        'grades/<slug:grade_slug>/import/template/',
        GradeStudentImportTemplateView.as_view(),
        name='grade_student_import_template',
    ),
    path(
        'streams/<slug:stream_slug>/roster/',
        StreamRosterView.as_view(),
        name='stream_roster',
    ),
    path(
        'streams/<slug:stream_slug>/finance/',
        StreamFinanceView.as_view(),
        name='stream_finance',
    ),
    path(
        'streams/<slug:stream_slug>/students/new/',
        StudentCreateView.as_view(),
        name='student_create',
    ),
    path(
        'streams/<slug:stream_slug>/students/<str:admission_number>/edit/',
        StudentEditView.as_view(),
        name='student_edit',
    ),
    path(
        'streams/<slug:stream_slug>/import/',
        StudentImportView.as_view(),
        name='student_import',
    ),
    path(
        'streams/<slug:stream_slug>/import/template/',
        StudentImportTemplateView.as_view(),
        name='student_import_template',
    ),
]
