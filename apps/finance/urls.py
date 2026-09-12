from django.urls import path

from finance.views import SchoolFinanceView, TermFeePlanView

app_name = 'finance'

urlpatterns = [
    path('', SchoolFinanceView.as_view(), name='overview'),
    path('plans/', TermFeePlanView.as_view(), name='term_fee_plans'),
]
