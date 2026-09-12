"""Twilio webhook request authentication helpers."""

from __future__ import annotations

import logging
import re

from django.conf import settings
from twilio.request_validator import RequestValidator

from communications.services.twilio_service import _normalize_e164

logger = logging.getLogger(__name__)


def build_twilio_webhook_url(request) -> str:
    """Absolute URL Twilio signed (prefer public SITE_DOMAIN behind tunnels)."""
    site = (getattr(settings, 'SITE_DOMAIN', '') or '').rstrip('/')
    if site:
        return f'{site}{request.get_full_path()}'
    return request.build_absolute_uri()


def verify_twilio_request(request) -> bool:
    """
    Validate X-Twilio-Signature against TWILIO_AUTH_TOKEN.
    Returns False when token missing or signature invalid.
    """
    auth_token = (getattr(settings, 'TWILIO_AUTH_TOKEN', '') or '').strip()
    if not auth_token:
        logger.warning('Twilio webhook rejected: TWILIO_AUTH_TOKEN not configured')
        return False

    signature = (request.META.get('HTTP_X_TWILIO_SIGNATURE') or '').strip()
    if not signature:
        logger.warning('Twilio webhook rejected: missing X-Twilio-Signature')
        return False

    validator = RequestValidator(auth_token)
    url = build_twilio_webhook_url(request)
    params = request.POST.dict()
    ok = validator.validate(url, params, signature)
    if not ok:
        logger.warning(
            'Twilio webhook signature invalid url=%s',
            url,
        )
    return ok


def webhook_to_matches_school(school, raw_to: str) -> bool:
    """
    If the school has a configured Twilio sender, require webhook To to match.
    Blank school number → allow (shared sandbox demo).
    """
    expected = (getattr(school, 'twilio_phone_number', None) or '').strip()
    if not expected:
        return True
    raw = (raw_to or '').strip()
    if not raw:
        return False
    if raw.lower().startswith('whatsapp:'):
        raw = raw.split(':', 1)[1]
    try:
        return _normalize_e164(raw) == _normalize_e164(expected)
    except ValueError:
        # Sandbox peer ids (e.g. KE.…) are not E.164 — compare digits loosely.
        digits = re.sub(r'\D', '', raw)
        expected_digits = re.sub(r'\D', '', expected)
        return bool(digits) and digits == expected_digits
