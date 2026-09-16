from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from finance.views import (
    daraja_c2b_confirmation,
    daraja_c2b_validation,
    daraja_callback,
)
from communications.views import twilio_whatsapp_webhook
from tenants.marketing_views import HowItWorksView, PricingView, PrivacyView
from tenants.views import LandingView, SignUpView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', LandingView.as_view(), name='landing'),
    path('pricing/', PricingView.as_view(), name='pricing'),
    path('how-it-works/', HowItWorksView.as_view(), name='how_it_works'),
    path('privacy/', PrivacyView.as_view(), name='privacy'),
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
        'api/v1/finance/daraja/c2b/validation/',
        daraja_c2b_validation,
        name='daraja_c2b_validation',
    ),
    path(
        'api/v1/finance/daraja/c2b/confirmation/',
        daraja_c2b_confirmation,
        name='daraja_c2b_confirmation',
    ),
    path(
        'api/v1/communications/twilio/webhook/',
        twilio_whatsapp_webhook,
        name='twilio_whatsapp_webhook',
    ),
    path('', include('dashboard.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
