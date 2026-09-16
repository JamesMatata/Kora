"""Symmetric encryption helpers for tenant secrets (Fernet)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet_from_material(material: str) -> Fernet:
    """Build a Fernet from a raw Fernet key or any passphrase."""
    raw = (material or '').strip()
    if not raw:
        raise ValueError('Empty encryption material.')
    try:
        return Fernet(raw.encode('utf-8'))
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode('utf-8')).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def _legacy_fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _primary_fernet() -> Fernet:
    explicit = (getattr(settings, 'KORA_CREDENTIALS_KEY', '') or '').strip()
    if explicit:
        return _fernet_from_material(explicit)
    return _legacy_fernet()


def _decrypt_fernets() -> list[Fernet]:
    """
    Prefer KORA_CREDENTIALS_KEY when set; always allow legacy SECRET_KEY
    decrypt so existing ciphertext remains readable during migration.
    """
    explicit = (getattr(settings, 'KORA_CREDENTIALS_KEY', '') or '').strip()
    if not explicit:
        return [_legacy_fernet()]
    return [_fernet_from_material(explicit), _legacy_fernet()]


def encrypt_value(plain: str) -> str:
    if not plain:
        return ''
    return _primary_fernet().encrypt(plain.encode('utf-8')).decode('utf-8')


def decrypt_value(token: str) -> str:
    if not token:
        return ''
    last_error: Exception | None = None
    for fernet in _decrypt_fernets():
        try:
            return fernet.decrypt(token.encode('utf-8')).decode('utf-8')
        except InvalidToken as exc:
            last_error = exc
    raise ValueError('Unable to decrypt stored credential.') from last_error
