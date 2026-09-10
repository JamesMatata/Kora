"""Deterministic circular avatars via DiceBear (Dylan style)."""

from __future__ import annotations

import json
from functools import lru_cache
from html import escape
from importlib.resources import files

from dicebear import Avatar, Style


@lru_cache(maxsize=1)
def _dylan_style() -> Style:
    definition = json.loads(
        files('dicebear_styles').joinpath('dylan.json').read_text('utf-8')
    )
    return Style(definition)


def avatar_seed_for(user) -> str:
    if user is None:
        return 'anonymous'
    email = getattr(user, 'email', '') or ''
    pk = getattr(user, 'pk', '') or ''
    return f'{email}:{pk}'.lower()


def avatar_alt(user) -> str:
    if user is None:
        return 'Avatar'
    name = ''
    get_full_name = getattr(user, 'get_full_name', None)
    if callable(get_full_name):
        name = (get_full_name() or '').strip()
    if not name:
        name = (getattr(user, 'email', '') or 'User').strip()
    return f'{name} avatar'


def build_avatar_data_uri(seed: str, size: int = 40) -> str:
    avatar = Avatar(
        _dylan_style(),
        {
            'seed': seed,
            'size': size,
            'borderRadius': 50,
        },
    )
    return avatar.to_data_uri()


def avatar_markup(user, size: int = 40, extra_class: str = '') -> str:
    seed = avatar_seed_for(user)
    data_uri = build_avatar_data_uri(seed, size=size)
    classes = 'inline-block shrink-0 overflow-hidden rounded-full bg-zinc-900'
    if extra_class:
        classes = f'{classes} {extra_class}'
    alt = escape(avatar_alt(user), quote=True)
    # quote data URI for HTML attribute safety (already mostly encoded)
    src = escape(data_uri, quote=True)
    return (
        f'<img src="{src}" alt="{alt}" width="{size}" height="{size}" '
        f'class="{classes}" style="width:{size}px;height:{size}px" '
        f'decoding="async" />'
    )
