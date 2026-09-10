"""Request/task-local school context for tenant-scoped queries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional, Union
from uuid import UUID

SchoolId = Union[UUID, str]
_current_school_id: ContextVar[Optional[SchoolId]] = ContextVar(
    'current_school_id',
    default=None,
)


def get_current_school_id() -> Optional[SchoolId]:
    return _current_school_id.get()


def set_current_school_id(school_id: Optional[SchoolId]) -> Token:
    return _current_school_id.set(school_id)


def clear_current_school_id(token: Optional[Token] = None) -> None:
    if token is not None:
        _current_school_id.reset(token)
    else:
        _current_school_id.set(None)


@contextmanager
def tenant_context(school_id: Optional[SchoolId]) -> Iterator[None]:
    token = set_current_school_id(school_id)
    try:
        yield
    finally:
        clear_current_school_id(token)
