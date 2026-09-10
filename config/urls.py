from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from finance.views import daraja_callback
from communications.views import twilio_whatsapp_webhook
from tenants.views import LandingView, SignUpView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', LandingView.as_view(), name='landing'),
    path(
        'accounts/login/',
        auth_views.LoginView.as_view(template_name='registration/login.html'),
        name='login',
    ),
    path(
        'accounts/logout/',
        auth_views.LogoutView.as_view(),
        name='logout',
    ),
    path('accounts/signup/', SignUpView.as_view(), name='signup'),
    path('tenants/', include('tenants.urls')),
    path('finance/', include('finance.urls')),
    path(
        'api/v1/finance/daraja/callback/',
        daraja_callback,
        name='daraja_callback',
    ),
    path(
        'api/v1/communications/twilio/webhook/',
        twilio_whatsapp_webhook,
        name='twilio_whatsapp_webhook',
    ),
    path('', include('dashboard.urls')),
]
