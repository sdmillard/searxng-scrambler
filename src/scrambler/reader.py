"""Whitelist-based HTML sanitizer for the Tor reader mode.

Rebuilds the document from scratch — only the explicitly listed tags and
per-tag attributes can appear in the output. All text is HTML-escaped.
Comments and entire subtrees of dangerous tags (script, style, nav, …)
are dropped. Relative URLs are resolved to absolute.
"""
import html
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# Tags whose output is allowed in the reader
_SAFE_TAGS = frozenset({
    'a', 'abbr', 'b', 'blockquote', 'br', 'caption', 'cite', 'code',
    'dd', 'details', 'dfn', 'dl', 'dt', 'em', 'figcaption', 'figure',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'ins',
    'li', 'mark', 'ol', 'p', 'pre', 'q', 's', 'section', 'small',
    'span', 'strong', 'sub', 'summary', 'sup', 'table', 'tbody',
    'td', 'th', 'thead', 'time', 'tr', 'u', 'ul',
})

# Tags whose entire subtree is dropped (including children)
_DROP_SUBTREE = frozenset({
    'script', 'style', 'nav', 'header', 'footer', 'aside', 'form',
    'iframe', 'object', 'embed', 'noscript', 'template', 'svg',
    'canvas', 'button', 'input', 'select', 'textarea',
})

_VOID_TAGS = frozenset({'br', 'hr', 'img'})


def _safe_url(raw: str, base: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if re.match(r'^(javascript|data|vbscript|blob):', raw, re.I):
        return None
    try:
        resolved = urljoin(base, raw)
        if resolved.startswith(('http://', 'https://')):
            return resolved
    except Exception:
        pass
    return None


def _walk(node, base_url: str) -> str:
    """Recursively rebuild safe HTML from a BeautifulSoup node."""
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return html.escape(str(node))
    if not isinstance(node, Tag):
        return ""

    name = (node.name or "").lower()

    if name in _DROP_SUBTREE:
        return ""

    inner = "".join(_walk(child, base_url) for child in node.children)

    if name not in _SAFE_TAGS:
        return inner  # pass children through, discard the wrapper tag

    attrs: dict = {}

    if name == 'a':
        href = _safe_url(node.get('href', ''), base_url)
        if href:
            attrs['href'] = href
            attrs['rel'] = 'noreferrer noopener'
            attrs['target'] = '_blank'
        if node.get('title'):
            attrs['title'] = html.escape(str(node['title']))

    elif name == 'img':
        src = _safe_url(node.get('src', ''), base_url)
        if not src:
            return inner  # drop <img> with no valid src
        attrs['src'] = src
        attrs['loading'] = 'lazy'
        attrs['alt'] = html.escape(str(node.get('alt', '')))

    elif name in ('td', 'th'):
        for attr in ('colspan', 'rowspan'):
            val = str(node.get(attr, ''))
            if re.match(r'^\d{1,4}$', val):
                attrs[attr] = val

    elif name == 'time':
        dt = node.get('datetime', '')
        if dt:
            attrs['datetime'] = html.escape(str(dt))

    attr_str = ''.join(f' {k}="{v}"' for k, v in attrs.items())
    if name in _VOID_TAGS:
        return f'<{name}{attr_str}>'
    return f'<{name}{attr_str}>{inner}</{name}>'


def extract_content(soup: BeautifulSoup, url: str) -> tuple[str, str]:
    """Return (title, sanitized_html) extracted from a parsed page."""
    title_el = soup.find('title')
    title = title_el.get_text(strip=True) if title_el else url

    content_el = (
        soup.find('article') or
        soup.find(attrs={'role': 'main'}) or
        soup.find('main') or
        soup.find(id=re.compile(r'\b(content|article|main|post|body)\b', re.I)) or
        soup.find(class_=re.compile(r'\b(article|content|main|post|entry|story)\b', re.I)) or
        soup.find('body') or
        soup
    )

    return title, _walk(content_el, url)
