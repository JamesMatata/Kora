"""Lightweight Markdown → safe HTML for executive briefings."""

from __future__ import annotations

import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


def _inline_format(text: str) -> str:
    text = escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(
        r'`([^`]+)`',
        (
            r'<code class="rounded bg-zinc-900 px-1 py-0.5 '
            r'font-mono text-xs text-yellow-400">\1</code>'
        ),
        text,
    )
    return text


@register.filter(name='simple_markdown')
def simple_markdown(value: str) -> str:
    """Render a constrained Markdown subset used by weekly AI summaries."""
    if not value:
        return ''

    lines = str(value).replace('\r\n', '\n').split('\n')
    html_parts: list[str] = []
    in_ul = False
    in_ol = False
    paragraph: list[str] = []

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            html_parts.append('</ul>')
            in_ul = False
        if in_ol:
            html_parts.append('</ol>')
            in_ol = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            joined = ' '.join(paragraph)
            html_parts.append(
                '<p class="mt-2 text-sm leading-relaxed text-zinc-300">'
                f'{_inline_format(joined)}</p>'
            )
            paragraph = []

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            flush_paragraph()
            close_lists()
            continue

        heading = re.match(r'^(#{1,3})\s+(.*)$', stripped)
        if heading:
            flush_paragraph()
            close_lists()
            level = len(heading.group(1))
            classes = {
                1: 'mt-5 text-xl font-semibold text-zinc-100',
                2: 'mt-4 text-lg font-semibold text-zinc-100',
                3: 'mt-3 text-sm font-semibold text-zinc-100',
            }[level]
            html_parts.append(
                f'<h{level} class="{classes}">'
                f'{_inline_format(heading.group(2))}</h{level}>'
            )
            continue

        ul_item = re.match(r'^[-*+]\s+(.*)$', stripped)
        if ul_item:
            flush_paragraph()
            if in_ol:
                html_parts.append('</ol>')
                in_ol = False
            if not in_ul:
                html_parts.append(
                    '<ul class="mt-2 list-disc space-y-1 pl-5 text-sm text-zinc-300">'
                )
                in_ul = True
            html_parts.append(f'<li>{_inline_format(ul_item.group(1))}</li>')
            continue

        ol_item = re.match(r'^(\d+)\.\s+(.*)$', stripped)
        if ol_item:
            flush_paragraph()
            if in_ul:
                html_parts.append('</ul>')
                in_ul = False
            if not in_ol:
                html_parts.append(
                    '<ol class="mt-2 list-decimal space-y-1 pl-5 text-sm text-zinc-300">'
                )
                in_ol = True
            html_parts.append(f'<li>{_inline_format(ol_item.group(2))}</li>')
            continue

        close_lists()
        paragraph.append(stripped)

    flush_paragraph()
    close_lists()
    return mark_safe('\n'.join(html_parts))
