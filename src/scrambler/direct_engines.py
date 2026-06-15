"""Direct engine fetchers — last-resort fallback when all SearXNG instances fail.

Supported engines: duckduckgo (DDG Lite), brave (API with key, then HTML),
mojeek, startpage, mwmbl.  Google and Bing are omitted: Bing's bot-detection
is too aggressive.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from . import circuits
from .fetcher import fetch
from .parser import Result


# ── DDG Lite ─────────────────────────────────────────────────────────────────

def _unwrap_ddg_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    if "uddg" in qs:
        return unquote(qs["uddg"][0])
    if href.startswith("http"):
        return href
    return ""


def _parse_ddg(html: str) -> list[Result]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[Result] = []
    for a in soup.find_all("a", class_="result-link"):
        url = _unwrap_ddg_url(a.get("href", ""))
        if not url:
            continue
        title = a.get_text(strip=True)
        snippet = ""
        tr = a.find_parent("tr")
        if tr:
            next_tr = tr.find_next_sibling("tr")
            if next_tr:
                td = next_tr.find("td", class_="result-snippet")
                if td:
                    snippet = td.get_text(strip=True)
        results.append(Result(url=url, title=title, snippet=snippet, engines=["duckduckgo"]))
    return results


def _fetch_ddg(
    query: str,
    pageno: int,
    tor_port: int,
    timeout: int,
    ft: int | None,
    use_tor: str,
) -> list[Result]:
    params: dict = {"q": query}
    if pageno > 1:
        params["s"] = str((pageno - 1) * 10)
    r = fetch(
        "https://lite.duckduckgo.com/lite/",
        params,
        tor_port=tor_port,
        timeout=timeout,
        routing=use_tor,
        tor_timeout=ft,
    )
    results = _parse_ddg(r.text) if r.ok else []
    if not results and use_tor == "tor_fallback":
        r2 = fetch(
            "https://lite.duckduckgo.com/lite/",
            params,
            timeout=timeout,
            routing="direct",
        )
        results = _parse_ddg(r2.text) if r2.ok else []
    return results


# ── Mojeek ───────────────────────────────────────────────────────────────────

def _parse_mojeek(html: str) -> list[Result]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[Result] = []
    for li in soup.select("ul.results-standard li"):
        title_a = li.select_one("h2 a.title")
        if not title_a:
            continue
        url = title_a.get("href", "") or title_a.get("title", "")
        title = title_a.get_text(strip=True)
        snippet_p = li.select_one("p.s")
        snippet = snippet_p.get_text(strip=True) if snippet_p else ""
        if url and title:
            results.append(Result(url=url, title=title, snippet=snippet, engines=["mojeek"]))
    return results


def _fetch_mojeek(
    query: str,
    pageno: int,
    tor_port: int,
    timeout: int,
    ft: int | None,
    use_tor: str,
) -> list[Result]:
    params: dict = {"q": query}
    if pageno > 1:
        params["s"] = str((pageno - 1) * 10 + 1)
    r = fetch(
        "https://www.mojeek.com/search",
        params,
        tor_port=tor_port,
        timeout=timeout,
        routing=use_tor,
        tor_timeout=ft,
    )
    results = _parse_mojeek(r.text) if r.ok else []
    if not results and use_tor == "tor_fallback":
        r2 = fetch(
            "https://www.mojeek.com/search",
            params,
            timeout=timeout,
            routing="direct",
        )
        results = _parse_mojeek(r2.text) if r2.ok else []
    return results


# ── Brave Search ──────────────────────────────────────────────────────────────

def _brave_api_request(
    params: dict,
    api_key: str,
    proxies: dict,
    timeout: int,
) -> list[Result]:
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            proxies=proxies,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            return [
                Result(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    snippet=item.get("description", ""),
                    engines=["brave"],
                )
                for item in data.get("web", {}).get("results", [])
                if item.get("url")
            ]
    except Exception:
        pass
    return []


def _parse_brave_html(html: str) -> list[Result]:
    """Parse Brave web search SSR HTML. Yields URL + title (no snippet in SSR)."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[Result] = []
    _brave_domains = {"search.brave.com", "imgs.search.brave.com", "tiles.search.brave.com"}
    for div in soup.find_all("div", attrs={"data-type": "web"}):
        # First external link is the result URL
        link = None
        for a in div.find_all("a", href=True):
            h = a["href"]
            if h.startswith("http") and urlparse(h).netloc not in _brave_domains:
                link = a
                break
        if not link:
            continue
        url = link["href"]
        # Title lives in the element with class search-snippet-title
        title_el = div.find(class_="search-snippet-title")
        title = title_el.get_text(strip=True) if title_el else ""
        if url and title:
            results.append(Result(url=url, title=title, snippet="", engines=["brave"]))
    return results


def _fetch_brave_html(
    query: str,
    pageno: int,
    tor_port: int,
    timeout: int,
    ft: int | None,
    use_tor: str,
) -> list[Result]:
    params: dict = {"q": query}
    if pageno > 1:
        params["offset"] = str((pageno - 1) * 10)
    r = fetch(
        "https://search.brave.com/search",
        params,
        tor_port=tor_port,
        timeout=timeout,
        routing=use_tor,
        tor_timeout=ft,
    )
    results = _parse_brave_html(r.text) if r.ok else []
    if not results and use_tor == "tor_fallback":
        r2 = fetch(
            "https://search.brave.com/search",
            params,
            timeout=timeout,
            routing="direct",
        )
        results = _parse_brave_html(r2.text) if r2.ok else []
    return results


def _fetch_brave(
    query: str,
    pageno: int,
    tor_port: int,
    timeout: int,
    ft: int | None,
    use_tor: str,
    api_key: str,
) -> list[Result]:
    # API path: full snippets, stable JSON contract
    if api_key:
        params = {"q": query, "count": 10, "offset": (pageno - 1) * 10}
        tor_proxies: dict = {}
        if use_tor in ("tor", "tor_fallback"):
            cred = circuits.get_credential()
            proxy = f"socks5h://{cred}:x@127.0.0.1:{tor_port}"
            tor_proxies = {"http": proxy, "https": proxy}
        results = _brave_api_request(params, api_key, tor_proxies, ft or timeout)
        if not results and use_tor == "tor_fallback":
            results = _brave_api_request(params, api_key, {}, timeout)
        if results:
            return results
    # HTML path: no API key needed; no snippets but URL + title are in SSR HTML
    return _fetch_brave_html(query, pageno, tor_port, timeout, ft, use_tor)


# ── Startpage ────────────────────────────────────────────────────────────────

def _parse_startpage(html: str) -> list[Result]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[Result] = []
    for div in soup.select("div.result"):
        title_el = div.select_one(".result-title")
        if not title_el:
            continue
        url = title_el.get("href", "").strip()
        title = title_el.get_text(strip=True)
        snippet_el = div.select_one("p")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        if url and title:
            results.append(Result(url=url, title=title, snippet=snippet, engines=["startpage"]))
    return results


def _fetch_startpage(
    query: str,
    pageno: int,
    tor_port: int,
    timeout: int,
    ft: int | None,
    use_tor: str,
) -> list[Result]:
    params: dict = {"q": query, "cat": "web"}
    if pageno > 1:
        params["page"] = str(pageno)
    r = fetch(
        "https://www.startpage.com/search",
        params,
        tor_port=tor_port,
        timeout=timeout,
        routing=use_tor,
        tor_timeout=ft,
    )
    results = _parse_startpage(r.text) if r.ok else []
    if not results and use_tor == "tor_fallback":
        r2 = fetch(
            "https://www.startpage.com/search",
            params,
            timeout=timeout,
            routing="direct",
        )
        results = _parse_startpage(r2.text) if r2.ok else []
    return results


# ── Mwmbl ────────────────────────────────────────────────────────────────────

def _fetch_mwmbl(
    query: str,
    pageno: int,
    tor_port: int,
    timeout: int,
    ft: int | None,
    use_tor: str,
) -> list[Result]:
    r = fetch(
        "https://api.mwmbl.org/search/",
        {"s": query},
        tor_port=tor_port,
        timeout=timeout,
        routing=use_tor,
        tor_timeout=ft,
    )
    results: list[Result] = []
    def _parse_mwmbl(text: str) -> list[Result]:
        out = []
        for item in json.loads(text):
            url = item.get("url", "")
            title = "".join(s["value"] for s in item.get("title", []))
            snippet = "".join(s["value"] for s in item.get("extract", []))
            if url and title:
                out.append(Result(url=url, title=title, snippet=snippet, engines=["mwmbl"]))
        return out

    if r.ok and r.text:
        try:
            results = _parse_mwmbl(r.text)
        except Exception:
            pass
    if not results and use_tor == "tor_fallback":
        r2 = fetch(
            "https://api.mwmbl.org/search/",
            {"s": query},
            timeout=timeout,
            routing="direct",
        )
        if r2.ok and r2.text:
            try:
                results = _parse_mwmbl(r2.text)
            except Exception:
                pass
    return results


# ── Dispatcher ────────────────────────────────────────────────────────────────

def search_direct_engines(
    engines: list[str],
    query: str,
    pageno,
    config,
    use_tor: str,
) -> list[Result]:
    """Query engines directly. Only runs when direct_engine_fallback pref is set."""
    if not config.prefs.get("direct_engine_fallback"):
        return []
    pn = int(pageno) if pageno else 1
    tor_port = config.tor_port
    timeout = config.request_timeout
    raw_ft = int(config.prefs.get("failover_timeout", 12))
    ft: int | None = raw_ft if raw_ft > 0 else None
    brave_key = config.prefs.get("brave_api_key", "").strip()

    results: list[Result] = []
    for eng in engines:
        eng_lower = eng.lower()
        if eng_lower == "duckduckgo":
            results.extend(_fetch_ddg(query, pn, tor_port, timeout, ft, use_tor))
        elif eng_lower == "brave":
            results.extend(_fetch_brave(query, pn, tor_port, timeout, ft, use_tor, brave_key))
        elif eng_lower == "mojeek":
            results.extend(_fetch_mojeek(query, pn, tor_port, timeout, ft, use_tor))
        elif eng_lower == "startpage":
            results.extend(_fetch_startpage(query, pn, tor_port, timeout, ft, use_tor))
        elif eng_lower == "mwmbl":
            results.extend(_fetch_mwmbl(query, pn, tor_port, timeout, ft, use_tor))
    return results
