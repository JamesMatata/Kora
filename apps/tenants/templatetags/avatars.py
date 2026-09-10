from django import template
from django.utils.safestring import mark_safe

from tenants.avatars import avatar_markup

register = template.Library()


@register.simple_tag
def user_avatar(user, size=36, class_name=''):
    """Render a deterministic circular avatar for a user."""
    try:
        size_int = int(size)
    except (TypeError, ValueError):
        size_int = 36
    return mark_safe(avatar_markup(user, size=size_int, extra_class=class_name))
