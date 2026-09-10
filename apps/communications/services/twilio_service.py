"""Outbound Twilio WhatsApp transmission."""

from __future__ import annotations

import logging
import re

from django.conf import settings
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from communications.models import MessageLog

logger = logging.getLogger(__name__)


class TwilioConfigError(Exception):
    """Raised when Twilio credentials or sender number are missing."""


def _normalize_e164(phone: str) -> str:
    """Return E.164 (+254…) for WhatsApp addressing."""
    raw = (phone or '').strip().replace(' ', '').replace('-', '')
    if not raw:
        raise ValueError('Phone number is required.')

    if raw.lower().startswith('whatsapp:'):
        raw = raw.split(':', 1)[1]

    if raw.startswith('00'):
        raw = f'+{raw[2:]}'
    elif raw.startswith('0') and len(raw) == 10:
        raw = f'+254{raw[1:]}'
    elif raw.startswith('254') and not raw.startswith('+'):
        raw = f'+{raw}'
    elif not raw.startswith('+'):
        raw = f'+{raw}'

    if not re.fullmatch(r'\+[1-9]\d{6,14}', raw):
        raise ValueError(f'Invalid E.164 phone number: {phone}')
    return raw


def _whatsapp_address(phone: str) -> str:
    e164 = _normalize_e164(phone)
    return f'whatsapp:{e164}'


def resolve_twilio_sender(school) -> str:
    """Prefer school.twilio_phone_number; else TWILIO_WHATSAPP_NUMBER from env."""
    school_number = (getattr(school, 'twilio_phone_number', None) or '').strip()
    fallback = (getattr(settings, 'TWILIO_WHATSAPP_NUMBER', '') or '').strip()
    number = school_number or fallback
    if not number:
        raise TwilioConfigError(
            'No WhatsApp sender configured '
            '(school.twilio_phone_number or TWILIO_WHATSAPP_NUMBER).'
        )
    return _normalize_e164(number)


def get_twilio_client() -> Client:
    account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '') or ''
    auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '') or ''
    if not account_sid or not auth_token:
        raise TwilioConfigError(
            'TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set.'
        )
    return Client(account_sid, auth_token)


def send_whatsapp_message(
    school,
    to_phone: str,
    body: str,
    session=None,
    sender_type: str = 'BOT',
) -> tuple[bool, str]:
    """
    Send a WhatsApp message via Twilio and optionally log it on a session.

    Returns (True, message_sid) or (False, error_message).
    """
    sender = (sender_type or MessageLog.Sender.BOT).upper()
    if sender not in MessageLog.Sender.values:
        sender = MessageLog.Sender.BOT

    message_log = None
    try:
        to_e164 = _normalize_e164(to_phone)
        from_e164 = resolve_twilio_sender(school)
    except (ValueError, TwilioConfigError) as exc:
        logger.warning('WhatsApp send blocked: %s', exc)
        return False, str(exc)

    if session is not None:
        message_log = MessageLog.objects.create(
            school=school,
            session=session,
            direction=MessageLog.Direction.OUTBOUND,
            sender=sender,
            body=body or '',
            delivery_status=MessageLog.DeliveryStatus.QUEUED,
        )

    try:
        client = get_twilio_client()
        twilio_message = client.messages.create(
            from_=_whatsapp_address(from_e164),
            to=_whatsapp_address(to_e164),
            body=body or '',
        )
    except TwilioRestException as exc:
        logger.warning(
            'Twilio WhatsApp failed school=%s to=%s: %s',
            getattr(school, 'id', None),
            to_e164,
            exc,
        )
        if message_log is not None:
            message_log.delivery_status = MessageLog.DeliveryStatus.FAILED
            message_log.save(update_fields=['delivery_status'])
        return False, str(exc.msg or exc)
    except TwilioConfigError as exc:
        logger.warning('Twilio config error: %s', exc)
        if message_log is not None:
            message_log.delivery_status = MessageLog.DeliveryStatus.FAILED
            message_log.save(update_fields=['delivery_status'])
        return False, str(exc)
    except Exception as exc:  # network / unexpected
        logger.exception(
            'Unexpected Twilio error school=%s to=%s',
            getattr(school, 'id', None),
            to_e164,
        )
        if message_log is not None:
            message_log.delivery_status = MessageLog.DeliveryStatus.FAILED
            message_log.save(update_fields=['delivery_status'])
        return False, str(exc)

    sid = twilio_message.sid or ''
    if message_log is not None:
        message_log.twilio_message_sid = sid or None
        message_log.delivery_status = MessageLog.DeliveryStatus.SENT
        message_log.save(
            update_fields=['twilio_message_sid', 'delivery_status']
        )

    return True, sid
