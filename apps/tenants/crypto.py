"""Symmetric encryption helpers for tenant secrets (Fernet)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(plain: str) -> str:
    if not plain:
        return ''
    return _fernet().encrypt(plain.encode('utf-8')).decode('utf-8')


def decrypt_value(token: str) -> str:
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise ValueError('Unable to decrypt stored credential.') from exc
