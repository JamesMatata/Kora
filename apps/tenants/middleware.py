from django.shortcuts import redirect
from django.urls import reverse

from tenants.context import clear_current_school_id, set_current_school_id
from tenants.models import SchoolMembership

# Paths that must not force an active school (avoid redirect loops).
TENANT_EXEMPT_PREFIXES = (
    '/accounts/login/',
    '/accounts/logout/',
    '/accounts/signup/',
    '/tenants/select/',
    '/tenants/create/',
    '/tenants/switch/',
    '/tenants/role/',
    '/tenants/profile/',
    '/tenants/inbox/',
    '/tenants/invitations/',
    '/tenants/notifications/',
    '/api/v1/finance/daraja/',
    '/api/v1/communications/twilio/',
    '/admin/',
    '/static/',
)


def _path_is_exempt(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in TENANT_EXEMPT_PREFIXES)


def _resolve_role_mode(request, membership):
    """
    Dual memberships can focus as admin or teacher via session.
    Single-role memberships are locked to that role.
    """
    is_admin = bool(membership.is_admin)
    is_teacher = bool(membership.is_teacher)
    can_switch = is_admin and is_teacher

    if can_switch:
        mode = request.session.get('active_role_mode', 'admin')
        if mode not in ('admin', 'teacher'):
            mode = 'admin'
        request.session['active_role_mode'] = mode
    elif is_admin:
        mode = 'admin'
        request.session.pop('active_role_mode', None)
    elif is_teacher:
        mode = 'teacher'
        request.session.pop('active_role_mode', None)
    else:
        mode = 'staff'
        request.session.pop('active_role_mode', None)

    request.can_switch_role = can_switch
    request.role_mode = mode
    # Capability flags (what the membership allows)
    request.is_current_school_admin = is_admin
    request.is_current_school_teacher = is_teacher
    # Effective UI flags (what the current mode shows)
    request.acting_as_admin = mode == 'admin' and is_admin
    request.acting_as_teacher = mode == 'teacher' and is_teacher


class TenantMiddleware:
    """
    Resolve the active school from the session membership, bind request
    helpers, and keep TenantAwareManager contextvars in sync.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.school = None
        request.membership = None
        request.is_current_school_admin = False
        request.is_current_school_teacher = False
        request.can_switch_role = False
        request.role_mode = None
        request.acting_as_admin = False
        request.acting_as_teacher = False
        request.user_memberships = []

        redirect_response = self._bind_tenant(request)
        school_id = request.school.pk if request.school is not None else None
        token = set_current_school_id(school_id)
        try:
            if redirect_response is not None:
                return redirect_response
            return self.get_response(request)
        finally:
            clear_current_school_id(token)

    def _bind_tenant(self, request):
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return None

        memberships = list(
            SchoolMembership.objects.filter(user=user, school__is_active=True)
            .select_related('school')
            .order_by('school__name')
        )
        request.user_memberships = memberships

        active_id = request.session.get('active_school_id')
        membership = None
        if active_id:
            membership = next(
                (m for m in memberships if str(m.school_id) == str(active_id)),
                None,
            )
            if membership is None:
                request.session.pop('active_school_id', None)

        if membership is None and len(memberships) == 1:
            membership = memberships[0]
            request.session['active_school_id'] = str(membership.school_id)

        if membership is not None:
            request.membership = membership
            request.school = membership.school
            _resolve_role_mode(request, membership)
            return None

        if _path_is_exempt(request.path):
            return None

        return redirect(reverse('tenants:select'))
