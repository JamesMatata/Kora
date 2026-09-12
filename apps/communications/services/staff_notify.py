"""Staff notification helpers for WhatsApp / finance ops."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Q

from tenants.models import SchoolMembership

User = get_user_model()


def finance_staff_users(school):
    """Admins and bursars who should see fee WhatsApp escalations / inbox."""
    user_ids = (
        SchoolMembership.objects.filter(school=school, user__is_active=True)
        .filter(Q(is_admin=True) | Q(is_bursar=True))
        .values_list('user_id', flat=True)
        .distinct()
    )
    return list(User.objects.filter(pk__in=user_ids))
