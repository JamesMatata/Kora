from academics.models import ClassStream
from django.utils import timezone

from tenants.models import Notification, StaffInvitation


def layout(request):
    """Provide global shell context from the active school membership + role mode."""
    user = getattr(request, 'user', None)
    empty = {
        'active_school': None,
        'is_admin': False,
        'is_bursar': False,
        'can_access_finance': False,
        'is_class_teacher': False,
        'can_switch_role': False,
        'role_mode': None,
        'role_label': '',
        'assigned_streams': [],
        'user_memberships': [],
        'pending_invitation_count': 0,
        'unread_notification_count': 0,
        'inbox_badge_count': 0,
    }

    if user is None or not user.is_authenticated:
        return empty

    membership = getattr(request, 'membership', None)
    active_school = getattr(request, 'school', None)
    can_switch = bool(getattr(request, 'can_switch_role', False))
    role_mode = getattr(request, 'role_mode', None)

    # Nav follows the active role mode for dual users.
    is_admin = bool(getattr(request, 'acting_as_admin', False))
    is_class_teacher = bool(getattr(request, 'acting_as_teacher', False))
    is_bursar = bool(getattr(request, 'is_current_school_bursar', False))
    can_access_finance = is_admin or is_bursar

    if can_switch:
        role_label = 'Admin' if role_mode == 'admin' else 'Teacher'
    elif membership is not None:
        role_label = membership.role_label
    elif is_admin:
        role_label = 'Admin'
    elif is_bursar:
        role_label = 'Bursar'
    elif is_class_teacher:
        role_label = 'Class Teacher'
    else:
        role_label = 'Staff'

    assigned_streams = []
    if active_school is not None and is_class_teacher:
        assigned_streams = list(
            ClassStream.objects.filter(
                school=active_school,
                class_teacher=user,
            ).select_related('grade_level')
        )

    pending_invitation_count = StaffInvitation.objects.filter(
        email__iexact=user.email,
        status=StaffInvitation.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).count()
    unread_notification_count = Notification.objects.filter(
        user=user,
        is_read=False,
    ).count()

    return {
        'active_school': active_school,
        'is_admin': is_admin,
        'is_bursar': is_bursar,
        'can_access_finance': can_access_finance,
        'is_class_teacher': is_class_teacher,
        'can_switch_role': can_switch,
        'role_mode': role_mode,
        'role_label': role_label,
        'assigned_streams': assigned_streams,
        'user_memberships': getattr(request, 'user_memberships', []),
        'pending_invitation_count': pending_invitation_count,
        'unread_notification_count': unread_notification_count,
        'inbox_badge_count': pending_invitation_count + unread_notification_count,
    }
