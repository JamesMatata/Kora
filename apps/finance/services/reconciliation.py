"""Deterministic Daraja STK callback reconciliation."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction

from finance.models import FeeInvoice, LedgerEntry, PaymentTransaction

logger = logging.getLogger(__name__)

ACK = {'ResultCode': 0, 'ResultDesc': 'Accepted'}

TERMINAL_STATUSES = frozenset(
    {
        PaymentTransaction.Status.SUCCESS,
        PaymentTransaction.Status.FAILED_USER,
        PaymentTransaction.Status.FAILED_TIMEOUT,
    }
)

# Safaricom result codes we map explicitly; anything else → FAILED_ERROR.
RESULT_USER_CANCELLED = 1032
RESULT_TIMEOUT = 1037
RESULT_INSUFFICIENT = 1


def _extract_stk_callback(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    body = payload.get('Body')
    if isinstance(body, dict) and isinstance(body.get('stkCallback'), dict):
        return body['stkCallback']
    if isinstance(payload.get('stkCallback'), dict):
        return payload['stkCallback']
    return {}


def _metadata_map(stk_callback: dict[str, Any]) -> dict[str, Any]:
    meta = stk_callback.get('CallbackMetadata') or {}
    items = meta.get('Item') or []
    out: dict[str, Any] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get('Name')
        if name:
            out[str(name)] = item.get('Value')
    return out


def _to_decimal(value) -> Decimal | None:
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _map_failure_status(result_code: int) -> str:
    if result_code == RESULT_USER_CANCELLED:
        return PaymentTransaction.Status.FAILED_USER
    if result_code == RESULT_TIMEOUT:
        return PaymentTransaction.Status.FAILED_TIMEOUT
    # Spec: ResultCode in [1032, 1037, 1] — map 1 and other codes to FAILED_ERROR
    return PaymentTransaction.Status.FAILED_ERROR


@transaction.atomic
def reconcile_stk_callback(
    *,
    payload: dict[str, Any],
    tenant_id: str | None = None,
) -> dict[str, str | int]:
    """
    Apply an STK callback to PaymentTransaction + FeeInvoice idempotently.

    Always safe to acknowledge to Safaricom after this returns.
    """
    stk = _extract_stk_callback(payload)
    checkout_id = (stk.get('CheckoutRequestID') or '').strip()
    if not checkout_id:
        logger.warning('Daraja callback missing CheckoutRequestID')
        return ACK

    try:
        result_code = int(stk.get('ResultCode'))
    except (TypeError, ValueError):
        logger.warning(
            'Daraja callback missing/invalid ResultCode checkout=%s',
            checkout_id,
        )
        return ACK

    result_desc = stk.get('ResultDesc') or ''

    qs = PaymentTransaction.objects.select_for_update().filter(
        checkout_request_id=checkout_id,
    )
    if tenant_id:
        qs = qs.filter(school_id=tenant_id)

    payment_tx = qs.first()
    if payment_tx is None:
        logger.info(
            'Daraja callback: no PaymentTransaction for checkout=%s tenant=%s',
            checkout_id,
            tenant_id,
        )
        return ACK

    if payment_tx.status in TERMINAL_STATUSES:
        return ACK

    payment_tx.raw_callback_payload = payload
    payment_tx.result_code = result_code
    payment_tx.result_desc = result_desc

    if result_code == 0:
        meta = _metadata_map(stk)
        receipt = (meta.get('MpesaReceiptNumber') or '').strip()
        amount = _to_decimal(meta.get('Amount'))
        if amount is None:
            amount = payment_tx.amount

        invoice = (
            FeeInvoice.objects.select_for_update()
            .select_related('student')
            .get(pk=payment_tx.invoice_id)
        )

        payment_tx.mpesa_receipt_number = receipt or None
        payment_tx.amount = amount
        payment_tx.status = PaymentTransaction.Status.SUCCESS
        # Avoid model save side-effects re-applying invoice balance; do it here.
        payment_tx.save(
            update_fields=[
                'raw_callback_payload',
                'result_code',
                'result_desc',
                'mpesa_receipt_number',
                'amount',
                'status',
                'updated_at',
            ]
        )

        LedgerEntry.create_from_transaction(payment_tx)

        invoice.paid_amount = (invoice.paid_amount or Decimal('0.00')) + amount
        if invoice.paid_amount >= invoice.total_amount:
            invoice.status = FeeInvoice.Status.PAID
        else:
            invoice.status = FeeInvoice.Status.PARTIALLY_PAID
        invoice.save(update_fields=['paid_amount', 'status', 'updated_at'])
        return ACK

    if result_code in (RESULT_USER_CANCELLED, RESULT_TIMEOUT, RESULT_INSUFFICIENT):
        payment_tx.status = _map_failure_status(result_code)
    else:
        payment_tx.status = PaymentTransaction.Status.FAILED_ERROR

    payment_tx.save(
        update_fields=[
            'raw_callback_payload',
            'result_code',
            'result_desc',
            'status',
            'updated_at',
        ]
    )
    return ACK
