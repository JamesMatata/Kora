from django.urls import path

from tenants.views import (
    InboxView,
    InvitationRespondView,
    NotificationMarkReadView,
    NotificationsMarkAllReadView,
    OpsSchoolReviewDetailView,
    OpsSchoolReviewListView,
    ProfileView,
    RoleModeSwitchView,
    SchoolBillingView,
    SignUpView,
    TenantCreateView,
    TenantSelectView,
    TenantSwitchView,
    VerificationPendingView,
)

app_name = 'tenants'

urlpatterns = [
    path('select/', TenantSelectView.as_view(), name='select'),
    path('create/', TenantCreateView.as_view(), name='create'),
    path(
        'verification/',
        VerificationPendingView.as_view(),
        name='verification_pending',
    ),
    path('billing/', SchoolBillingView.as_view(), name='billing'),
    path('ops/applications/', OpsSchoolReviewListView.as_view(), name='ops_review_list'),
    path(
        'ops/applications/<uuid:school_id>/',
        OpsSchoolReviewDetailView.as_view(),
        name='ops_review_detail',
    ),
    path('switch/<uuid:school_id>/', TenantSwitchView.as_view(), name='switch'),
    path('role/<str:mode>/', RoleModeSwitchView.as_view(), name='role_mode'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('inbox/', InboxView.as_view(), name='inbox'),
    path(
        'invitations/<uuid:invitation_id>/<str:decision>/',
        InvitationRespondView.as_view(),
        name='invitation_respond',
    ),
    path(
        'notifications/<uuid:notification_id>/read/',
        NotificationMarkReadView.as_view(),
        name='notification_read',
    ),
    path(
        'notifications/read-all/',
        NotificationsMarkAllReadView.as_view(),
        name='notifications_read_all',
    ),
]
