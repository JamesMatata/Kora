from functools import wraps
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


UNAUTHORIZED_ADMIN_MESSAGE = 'Unauthorized: Requires administrator credentials'
SWITCH_TO_ADMIN_MESSAGE = 'Switch to Admin mode to access school administration.'
SWITCH_TO_TEACHER_MESSAGE = 'Switch to Teacher mode to access class teaching tools.'


def _safe_redirect_target(request):
    referer = request.META.get('HTTP_REFERER', '')
    if referer:
        parsed = urlparse(referer)
        if parsed.path and (not parsed.netloc or parsed.netloc == request.get_host()):
            return parsed.path
    return '/'


def _deny(request, message=UNAUTHORIZED_ADMIN_MESSAGE):
    messages.error(request, message)
    return redirect(_safe_redirect_target(request))


def school_admin_required(view_func):
    """Require admin membership, and Admin mode when the user is dual-role."""

    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not getattr(request, 'is_current_school_admin', False):
            return _deny(request)
        if (
            getattr(request, 'can_switch_role', False)
            and getattr(request, 'role_mode', None) != 'admin'
        ):
            return _deny(request, SWITCH_TO_ADMIN_MESSAGE)
        return view_func(request, *args, **kwargs)

    return _wrapped


def teacher_required(view_func):
    """Require teacher membership, and Teacher mode when the user is dual-role."""

    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not getattr(request, 'is_current_school_teacher', False):
            return _deny(request)
        if (
            getattr(request, 'can_switch_role', False)
            and getattr(request, 'role_mode', None) != 'teacher'
        ):
            return _deny(request, SWITCH_TO_TEACHER_MESSAGE)
        return view_func(request, *args, **kwargs)

    return _wrapped
