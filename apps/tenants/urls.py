from django.urls import path

from tenants.views import (
    InboxView,
    InvitationRespondView,
    NotificationMarkReadView,
    NotificationsMarkAllReadView,
    ProfileView,
    RoleModeSwitchView,
    SignUpView,
    TenantCreateView,
    TenantSelectView,
    TenantSwitchView,
)

app_name = 'tenants'

urlpatterns = [
    path('select/', TenantSelectView.as_view(), name='select'),
    path('create/', TenantCreateView.as_view(), name='create'),
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
