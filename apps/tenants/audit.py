"""Write append-only school audit events."""

from __future__ import annotations

from tenants.models import AuditEvent


def log_audit_event(
    school,
    *,
    category: str,
    action: str,
    summary: str,
    actor=None,
    object_type: str = '',
    object_id: str = '',
    metadata: dict | None = None,
) -> AuditEvent:
    return AuditEvent.objects.create(
        school=school,
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
        category=category,
        action=action,
        summary=summary[:255],
        object_type=(object_type or '')[:64],
        object_id=str(object_id or '')[:64],
        metadata=metadata or {},
    )
