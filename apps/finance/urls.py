from django.urls import path

from dashboard.views import (
    DefaulterExportView,
    DefaulterReportView,
    FeeLedgerSearchView,
    FeeLedgerStkPushView,
    FeeLedgerView,
    InvoiceDiscountView,
    InvoiceManualPaymentView,
    PaymentPromisesView,
    PaymentReceiptPdfView,
    PaymentReceiptView,
    PaymentReceiptWhatsAppView,
    PausedRemindersView,
    StudentFeeStatementView,
)
from finance.views import TermFeePlanCreateView, TermFeePlanView

app_name = 'finance'

urlpatterns = [
    path('', FeeLedgerView.as_view(), name='fee_ledger'),
    path('search/', FeeLedgerSearchView.as_view(), name='fee_ledger_search'),
    path('stk/', FeeLedgerStkPushView.as_view(), name='fee_ledger_stk'),
    path('defaulters/', DefaulterReportView.as_view(), name='defaulter_report'),
    path(
        'defaulters/export/',
        DefaulterExportView.as_view(),
        name='defaulter_export',
    ),
    path('promises/', PaymentPromisesView.as_view(), name='payment_promises'),
    path(
        'paused-reminders/',
        PausedRemindersView.as_view(),
        name='paused_reminders',
    ),
    path(
        'students/<str:admission_number>/statement/',
        StudentFeeStatementView.as_view(),
        name='student_fee_statement',
    ),
    path(
        'students/<str:admission_number>/discount/',
        InvoiceDiscountView.as_view(),
        name='invoice_discount',
    ),
    path(
        'students/<str:admission_number>/manual-payment/',
        InvoiceManualPaymentView.as_view(),
        name='invoice_manual_payment',
    ),
    path(
        'receipts/<int:payment_id>/',
        PaymentReceiptView.as_view(),
        name='payment_receipt',
    ),
    path(
        'receipts/<int:payment_id>/pdf/',
        PaymentReceiptPdfView.as_view(),
        name='payment_receipt_pdf',
    ),
    path(
        'receipts/<int:payment_id>/whatsapp/',
        PaymentReceiptWhatsAppView.as_view(),
        name='payment_receipt_whatsapp',
    ),
    path('plans/', TermFeePlanView.as_view(), name='term_fee_plans'),
    path('plans/new/', TermFeePlanCreateView.as_view(), name='term_fee_plan_create'),
]
