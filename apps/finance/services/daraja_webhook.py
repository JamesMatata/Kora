"""Authenticate Safaricom Daraja callback requests (token + optional IP)."""

from __future__ import annotations

import hmac
import ipaddress
import logging

from django.conf import settings
from django.http import HttpRequest

logger = logging.getLogger(__name__)


def _client_ip(request: HttpRequest) -> str:
    forwarded = (request.META.get('HTTP_X_FORWARDED_FOR') or '').strip()
    if forwarded:
        return forwarded.split(',')[0].strip()
    return (request.META.get('REMOTE_ADDR') or '').strip()


def ip_allowed(request: HttpRequest) -> bool:
    """
    If DARAJA_CALLBACK_IP_ALLOWLIST is empty, allow all clients.
    Otherwise require the peer (or first X-Forwarded-For hop) to match.
    """
    allowlist = getattr(settings, 'DARAJA_CALLBACK_IP_ALLOWLIST', None) or []
    if not allowlist:
        return True
    client = _client_ip(request)
    if not client:
        return False
    try:
        addr = ipaddress.ip_address(client)
    except ValueError:
        return False
    for entry in allowlist:
        entry = (entry or '').strip()
        if not entry:
            continue
        try:
            if '/' in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def authenticate_daraja_callback(request: HttpRequest):
    """
    Resolve and authorize the school for a Daraja webhook.

    Requires ?tenant_id=<uuid>&token=<webhook secret>.
    Returns the School on success, otherwise None.
    """
    from tenants.models import School

    if not ip_allowed(request):
        logger.warning(
            'Daraja callback rejected: IP not allowlisted ip=%s',
            _client_ip(request),
        )
        return None

    tenant_id = (request.GET.get('tenant_id') or '').strip()
    token = (request.GET.get('token') or '').strip()
    if not tenant_id or not token:
        logger.warning('Daraja callback missing tenant_id or token')
        return None

    school = School.objects.filter(pk=tenant_id, is_active=True).first()
    if school is None:
        logger.warning('Daraja callback unknown school=%s', tenant_id)
        return None

    try:
        expected = school.get_daraja_webhook_token()
    except ValueError:
        logger.warning(
            'Daraja callback: cannot decrypt webhook secret school=%s',
            tenant_id,
        )
        return None

    if not expected or not hmac.compare_digest(expected, token):
        logger.warning(
            'Daraja callback invalid token school=%s',
            tenant_id,
        )
        return None

    return school
