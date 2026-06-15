import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup


@dataclass
class Result:
    url: str
    title: str
    snippet: str
    engines: list = field(default_factory=list)
    instance_count: int = 1   # how many SearXNG instances returned this URL
    priority_source: str | None = None
    priority_rank: int | None = None
    thumbnail: str | None = None   # absolute thumbnail URL (proxied via SearXNG)
    duration: str | None = None    # video/music length string e.g. "3:45"
    img_src: str | None = None     # full-size image URL (images category only)
    date: datetime | None = None   # publication date extracted from result HTML
    result_type: str = "general"   # "general" | "image" | "music" | "file"


@dataclass
class ParsedPage:
    results: list
    failed_engines: list


def parse(html: str, base_url: str = "") -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    return ParsedPage(
        results=_parse_results(soup, base_url),
        failed_engines=_parse_failed_engines(soup),
    )


def _parse_results(soup: BeautifulSoup, base_url: str = "") -> list:
    results = []
    for article in soup.select("article.result"):
        classes = set(article.get("class") or [])
        if "result-images" in classes:
            r = _parse_image_result(article, base_url)
        else:
            r = _parse_default_result(article, base_url)
            if r:
                if "result-music" in classes:
                    r.result_type = "music"
                elif classes & {"result-torrent", "result-files", "result-file"}:
                    r.result_type = "file"
        if r:
            results.append(r)
    return results


def _parse_default_result(article, base_url: str) -> "Result | None":
    url = _extract_url(article)
    title = _extract_title(article)
    if not url or not title:
        return None
    return Result(
        url=url,
        title=title,
        snippet=_extract_snippet(article),
        engines=_extract_engines(article),
        thumbnail=_extract_thumbnail(article, base_url),
        duration=_extract_duration(article),
        date=_extract_date(article),
    )


def _parse_image_result(article, base_url: str) -> "Result | None":
    # Title is in <span class="title">, NOT in <h3>
    title_el = article.select_one("span.title")
    title = title_el.get_text(strip=True) if title_el else None

    # Full-size image URL: outer <a href> links to result.img_src
    outer_a = article.find("a")
    img_src = outer_a.get("href", "") if outer_a else ""

    # Source webpage URL: inside the detail panel
    url_el = article.select_one(".detail .result-url a, p.result-url a")
    url = url_el.get("href", "") if url_el else img_src

    if not url or not title:
        return None

    # Thumbnail: <img class="image_thumbnail"> with relative src — unwrap SearXNG proxy to CDN URL
    thumb_img = article.select_one("img.image_thumbnail")
    thumbnail = None
    if thumb_img:
        src = thumb_img.get("src") or ""
        thumbnail = _unwrap_proxy(_make_absolute(src, base_url)) or None

    snippet_el = article.select_one(".result-images-labels .result-content, p.result-content, .content")
    snippet = snippet_el.get_text(strip=True) if snippet_el else ""

    return Result(
        url=url,
        title=title,
        snippet=snippet,
        engines=_extract_engines(article),
        thumbnail=thumbnail,
        img_src=img_src if img_src.startswith("http") else None,
        date=_extract_date(article),
    )


def _extract_url(article) -> str | None:
    if article.get("data-url"):
        return article["data-url"]
    for a in article.select("h3 a, a.url_header"):
        href = a.get("href", "")
        if href.startswith("http"):
            return href
    return None


def _extract_title(article) -> str | None:
    a = article.select_one("h3 a")
    return a.get_text(strip=True) if a else None


def _extract_snippet(article) -> str:
    p = article.select_one("p.content, .content")
    if not p:
        return ""
    # Preserve SearXNG's <mark> highlights as <b> so matched terms stay visible
    parts = []
    for node in p.children:
        if hasattr(node, "name"):
            if node.name == "mark":
                parts.append("<b>" + _html.escape(node.get_text()) + "</b>")
            else:
                parts.append(_html.escape(node.get_text()))
        else:
            parts.append(_html.escape(str(node)))
    return "".join(parts).strip()


def _make_absolute(src: str, base_url: str) -> str:
    if not src:
        return ""
    if src.startswith("http"):
        return src
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/") and base_url:
        return base_url.rstrip("/") + src
    return src


def _unwrap_proxy(url: str) -> str:
    """If url is a SearXNG /image_proxy?url=<cdn-url>&h=<hmac>, return the inner CDN URL.
    This lets thumbnails load directly from the CDN rather than through SearXNG's proxy,
    which often requires session cookies or a matching Referer to work."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if parsed.path.rstrip("/") in ("/image_proxy", "/imageproxy"):
            inner = parse_qs(parsed.query).get("url", [None])[0]
            if inner:
                decoded = unquote(inner)
                if decoded.startswith("http"):
                    return decoded
    except Exception:
        pass
    return url


def _extract_thumbnail(article, base_url: str) -> str | None:
    # Videos use <img class="thumbnail">, other result types may vary
    thumb = article.select_one("img.thumbnail, img.image_thumbnail")
    if not thumb:
        return None
    src = thumb.get("src") or thumb.get("data-src") or ""
    absolute = _make_absolute(src, base_url)
    return _unwrap_proxy(absolute) or None


def _extract_duration(article) -> str | None:
    el = article.select_one("span.thumbnail_length, div.result_length, .result_length")
    if not el:
        return None
    text = el.get_text(strip=True)
    # Strip "Length: " prefix SearXNG sometimes adds
    for prefix in ("Length:", "length:", "Länge:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text or None


def _extract_engines(article) -> list:
    engines_el = article.select_one(".engines, div.engines, p.engines")
    if engines_el:
        names = []
        for span in engines_el.select("span, a"):
            name = span.get_text(strip=True).lower()
            if name and name not in names:
                names.append(name)
        if names:
            return names
    # SearXNG also encodes engines in the article's own data-engines attribute
    # (image results often skip the .engines paragraph entirely)
    data_attr = article.get("data-engines", "")
    if data_attr:
        return [e.strip().lower() for e in data_attr.split(",") if e.strip()]
    return []


# ── Date extraction ───────────────────────────────────────────────────────────

_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def _extract_date(article) -> datetime | None:
    # <time datetime="..."> — most reliable when present
    for el in article.select("time[datetime]"):
        dt = _parse_date_str(el.get("datetime", ""))
        if dt:
            return dt
    # <time> with text content
    for el in article.select("time"):
        dt = _parse_date_str(el.get_text(strip=True))
        if dt:
            return dt
    # SearXNG simple theme date classes
    for el in article.select(".published_date, .result-pubdate, .date"):
        dt = _parse_date_str(el.get_text(strip=True))
        if dt:
            return dt
    return None


def _parse_date_str(s: str) -> datetime | None:
    """Parse a date string into an aware datetime (UTC). Returns None on failure."""
    if not s:
        return None
    s = s.strip()

    # ISO 8601: 2025-01-15 or 2025-01-15T10:00:00
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            pass

    # "Jan 15, 2025" or "January 15, 2025"
    m = re.match(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', s)
    if m:
        mon = _MONTHS.get(m.group(1)[:3].lower())
        if mon:
            try:
                return datetime(int(m.group(3)), mon, int(m.group(2)), tzinfo=timezone.utc)
            except ValueError:
                pass

    # "15 Jan 2025"
    m = re.match(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon:
            try:
                return datetime(int(m.group(3)), mon, int(m.group(1)), tzinfo=timezone.utc)
            except ValueError:
                pass

    # Relative: "2 days ago", "3 hours ago"
    now = datetime.now(timezone.utc)
    m = re.match(r'(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago', s.lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            'second': timedelta(seconds=n), 'minute': timedelta(minutes=n),
            'hour':   timedelta(hours=n),   'day':    timedelta(days=n),
            'week':   timedelta(weeks=n),   'month':  timedelta(days=n * 30),
            'year':   timedelta(days=n * 365),
        }.get(unit)
        if delta:
            return now - delta

    if s.lower() == 'yesterday':
        return now - timedelta(days=1)
    if s.lower() in ('today', 'just now'):
        return now

    return None


def _parse_failed_engines(soup: BeautifulSoup) -> list:
    failed = []

    # Primary location in SearXNG simple theme
    for li in soup.select("#engine-warnings li, #engine-errors li"):
        strong = li.select_one("strong, b")
        if strong:
            name = strong.get_text(strip=True).lower()
            if name and name not in failed:
                failed.append(name)

    # Fallback: scan alert-warning blocks
    for block in soup.select(".alert-warning, .alert-danger"):
        for li in block.select("li"):
            strong = li.select_one("strong, b")
            if strong:
                name = strong.get_text(strip=True).lower()
                if name and name not in failed:
                    failed.append(name)

    return failed
