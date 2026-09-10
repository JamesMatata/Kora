"""Safaricom Daraja (M-Pesa) client — multi-tenant credential aware."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from finance.models import FeeInvoice, PaymentTransaction

logger = logging.getLogger(__name__)

NAIROBI = ZoneInfo('Africa/Nairobi')
TOKEN_CACHE_SECONDS = 3000  # 50 minutes
DEFAULT_TIMEOUT = 30
SANDBOX_SHORTCODE = '174379'

OAUTH_URL = 'https://sandbox.safaricom.co.ke/oauth/v1/generate'
STK_PUSH_URL = 'https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest'


class DarajaError(Exception):
    """Raised when credential resolution or Daraja API setup fails."""


@dataclass(frozen=True)
class DarajaCredentials:
    consumer_key: str
    consumer_secret: str
    passkey: str
    paybill_number: str


def normalize_msisdn(phone_number: str) -> str:
    """Normalize to Safaricom STK format: 2547XXXXXXXX / 2541XXXXXXXX."""
    raw = (phone_number or '').strip().replace(' ', '').replace('-', '')
    if raw.startswith('+'):
        raw = raw[1:]
    if raw.startswith('0') and len(raw) == 10:
        raw = f'254{raw[1:]}'
    if not raw.startswith('254') or len(raw) != 12 or raw[3] not in ('7', '1'):
        raise DarajaError(
            'Phone number must be a valid Kenyan MSISDN '
            '(2547XXXXXXXX or 2541XXXXXXXX).'
        )
    return raw


def resolve_credentials(school) -> DarajaCredentials:
    """
    Prefer school-encrypted Daraja keys; fall back to project .env sandbox keys.
    """
    school_creds = school.get_mpesa_credentials()
    consumer_key = school_creds['consumer_key'] or getattr(
        settings, 'DARAJA_CONSUMER_KEY', ''
    )
    consumer_secret = school_creds['consumer_secret'] or getattr(
        settings, 'DARAJA_CONSUMER_SECRET', ''
    )
    passkey = school_creds['passkey'] or getattr(settings, 'DARAJA_PASSKEY', '')
    paybill = school_creds['paybill_number'] or getattr(
        settings, 'DARAJA_SHORTCODE', SANDBOX_SHORTCODE
    )

    missing = [
        name
        for name, value in (
            ('consumer_key', consumer_key),
            ('consumer_secret', consumer_secret),
            ('passkey', passkey),
            ('paybill_number', paybill),
        )
        if not value
    ]
    if missing:
        raise DarajaError(
            f'Daraja credentials incomplete for school {school.code}: '
            f'missing {", ".join(missing)}.'
        )

    return DarajaCredentials(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        passkey=passkey,
        paybill_number=str(paybill).strip(),
    )


def _token_cache_key(school) -> str:
    return f'daraja_token_{school.id}'


def get_access_token(school, *, session: requests.Session | None = None) -> str:
    """Return a cached OAuth access token for the school (50 min TTL)."""
    cache_key = _token_cache_key(school)
    cached = cache.get(cache_key)
    if cached:
        return cached

    creds = resolve_credentials(school)
    http = session or requests
    try:
        response = http.get(
            OAUTH_URL,
            params={'grant_type': 'client_credentials'},
            auth=(creds.consumer_key, creds.consumer_secret),
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise DarajaError('Daraja OAuth request timed out.') from exc
    except requests.RequestException as exc:
        raise DarajaError(f'Daraja OAuth network error: {exc}') from exc

    if response.status_code != 200:
        raise DarajaError(
            f'Daraja OAuth failed ({response.status_code}): {response.text[:300]}'
        )

    try:
        payload = response.json()
        token = payload['access_token']
    except (ValueError, KeyError, TypeError) as exc:
        raise DarajaError('Daraja OAuth response missing access_token.') from exc

    cache.set(cache_key, token, TOKEN_CACHE_SECONDS)
    return token


def _stk_password(paybill: str, passkey: str, timestamp: str) -> str:
    raw = f'{paybill}{passkey}{timestamp}'.encode('utf-8')
    return base64.b64encode(raw).decode('utf-8')


def _nairobi_timestamp() -> str:
    return timezone.now().astimezone(NAIROBI).strftime('%Y%m%d%H%M%S')


def _callback_url(school) -> str:
    domain = getattr(settings, 'SITE_DOMAIN', '').rstrip('/')
    if not domain:
        raise DarajaError('SITE_DOMAIN is not configured.')
    return f'{domain}/api/v1/finance/daraja/callback/?tenant_id={school.id}'


def initiate_stk_push(
    school,
    invoice: FeeInvoice,
    phone_number: str,
    amount: Decimal | int | str,
    *,
    session: requests.Session | None = None,
) -> tuple[bool, PaymentTransaction, dict[str, Any]]:
    """
    Start an STK push for an invoice.

    Returns (ok, payment_transaction, response_json).
    """
    if invoice.school_id != school.id:
        raise DarajaError('Invoice does not belong to the given school.')

    creds = resolve_credentials(school)
    msisdn = normalize_msisdn(phone_number)
    amount_int = int(Decimal(str(amount)))
    if amount_int < 1:
        raise DarajaError('Amount must be at least 1.')

    timestamp = _nairobi_timestamp()
    password = _stk_password(creds.paybill_number, creds.passkey, timestamp)
    admission = invoice.student.admission_number

    payment_tx = PaymentTransaction.objects.create(
        school=school,
        invoice=invoice,
        phone_number=f'+{msisdn}',
        amount=Decimal(amount_int),
        status=PaymentTransaction.Status.INITIALIZED,
    )

    payload = {
        'BusinessShortCode': creds.paybill_number,
        'Password': password,
        'Timestamp': timestamp,
        'TransactionType': 'CustomerPayBillOnline',
        'Amount': amount_int,
        'PartyA': msisdn,
        'PartyB': creds.paybill_number,
        'PhoneNumber': msisdn,
        'CallBackURL': _callback_url(school),
        'AccountReference': f'ADM-{admission}'[:12],
        'TransactionDesc': f'Fee {admission}'[:12],
    }

    response_json: dict[str, Any] = {}
    http = session or requests
    try:
        token = get_access_token(school, session=session)
        response = http.post(
            STK_PUSH_URL,
            json=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except DarajaError as exc:
        payment_tx.status = PaymentTransaction.Status.FAILED_ERROR
        payment_tx.result_desc = str(exc)
        payment_tx.save(
            update_fields=['status', 'result_desc', 'updated_at']
        )
        return False, payment_tx, {'error': str(exc)}
    except requests.Timeout:
        payment_tx.status = PaymentTransaction.Status.FAILED_TIMEOUT
        payment_tx.result_desc = 'STK push request timed out.'
        payment_tx.save(
            update_fields=['status', 'result_desc', 'updated_at']
        )
        return False, payment_tx, {'error': 'timeout'}
    except requests.RequestException as exc:
        logger.exception('Daraja STK network error for school=%s', school.id)
        payment_tx.status = PaymentTransaction.Status.FAILED_ERROR
        payment_tx.result_desc = f'Network error: {exc}'
        payment_tx.save(
            update_fields=['status', 'result_desc', 'updated_at']
        )
        return False, payment_tx, {'error': str(exc)}

    try:
        response_json = response.json() if response.content else {}
    except ValueError:
        response_json = {'raw': response.text[:500]}

    if response.status_code != 200:
        payment_tx.status = PaymentTransaction.Status.FAILED_ERROR
        payment_tx.result_code = response.status_code
        payment_tx.result_desc = (
            response_json.get('errorMessage')
            or response_json.get('ResponseDescription')
            or response.text[:500]
        )
        payment_tx.raw_callback_payload = response_json
        payment_tx.save(
            update_fields=[
                'status',
                'result_code',
                'result_desc',
                'raw_callback_payload',
                'updated_at',
            ]
        )
        return False, payment_tx, response_json

    response_code = str(response_json.get('ResponseCode', ''))
    if response_code == '0':
        payment_tx.checkout_request_id = response_json.get('CheckoutRequestID') or None
        payment_tx.merchant_request_id = response_json.get('MerchantRequestID') or None
        payment_tx.status = PaymentTransaction.Status.STK_PUSH_SENT
        payment_tx.result_desc = response_json.get('ResponseDescription') or ''
        payment_tx.raw_callback_payload = response_json
        payment_tx.save(
            update_fields=[
                'checkout_request_id',
                'merchant_request_id',
                'status',
                'result_desc',
                'raw_callback_payload',
                'updated_at',
            ]
        )
        return True, payment_tx, response_json

    payment_tx.status = PaymentTransaction.Status.FAILED_ERROR
    payment_tx.result_desc = (
        response_json.get('ResponseDescription')
        or response_json.get('errorMessage')
        or 'STK push was not accepted.'
    )
    payment_tx.raw_callback_payload = response_json
    payment_tx.save(
        update_fields=[
            'status',
            'result_desc',
            'raw_callback_payload',
            'updated_at',
        ]
    )
    return False, payment_tx, response_json
