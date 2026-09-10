from django.urls import path

from finance.views import SchoolFinanceView

app_name = 'finance'

urlpatterns = [
    path('', SchoolFinanceView.as_view(), name='overview'),
]
