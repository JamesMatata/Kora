"""Record scheduled job outcomes for Settings / ops visibility."""

from __future__ import annotations

from django.utils import timezone

from tenants.models import OpsJobRun


def record_ops_job(
    *,
    job_name: str,
    status: str,
    summary: str = '',
    detail: str = '',
    school=None,
    started_at=None,
) -> OpsJobRun:
    return OpsJobRun.objects.create(
        job_name=job_name,
        school=school,
        status=status,
        summary=(summary or '')[:255],
        detail=detail or '',
        started_at=started_at or timezone.now(),
    )


def latest_ops_job(job_name: str, *, school=None):
    qs = OpsJobRun.objects.filter(job_name=job_name)
    if school is not None:
        qs = qs.filter(school=school)
    else:
        qs = qs.filter(school__isnull=True)
    return qs.first()
