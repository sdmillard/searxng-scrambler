from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from .parser import Result

_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "msclkid", "ref", "source", "via",
    "_ga", "_gl", "mc_eid", "twclid", "igshid", "si",
})


def _norm_url(url: str) -> str:
    """Normalize URL for deduplication: drop www, force https, strip tracking
    params and fragments, sort remaining params, strip trailing slash."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
        path = p.path.rstrip("/") or "/"
        qs = sorted(
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in _STRIP_PARAMS
        )
        return urlunparse(("https", host + port, path, "", urlencode(qs), ""))
    except Exception:
        return url.lower().strip()


def merge(primary: list, supplement: list) -> list:
    """Deduplicate by normalized URL; results appearing in more engines rank higher."""
    positions: dict = {}
    by_norm: dict = {}

    for pos, r in enumerate(primary):
        nurl = _norm_url(r.url)
        by_norm[nurl] = r
        positions[nurl] = pos

    for r in supplement:
        nurl = _norm_url(r.url)
        if nurl in by_norm:
            existing = by_norm[nurl]
            existing.instance_count += 1
            for e in r.engines:
                if e not in existing.engines:
                    existing.engines.append(e)
        else:
            by_norm[nurl] = r
            positions[nurl] = len(primary) + len(by_norm)

    results = list(by_norm.values())
    results.sort(key=lambda r: (-len(r.engines), positions.get(_norm_url(r.url), 9999)))
    return results


def merge_ranked(results_lists: list, normalize: bool = True) -> list:
    """Merge results from multiple instances using Borda count scoring.

    Each instance awards (N - rank) points to its Nth result. A result
    appearing near the top of multiple instances scores highest.
    When normalize=True, URLs are normalized before dedup so www/non-www
    and tracking params don't create duplicates; original URL is preserved
    for display. When normalize=False, exact URL matching is used.
    instance_count tracks how many distinct instances returned each URL.
    """
    key_of = _norm_url if normalize else (lambda u: u)

    scores: dict = {}   # key -> borda score
    by_key: dict = {}   # key -> Result (first-seen, original URL preserved)

    for results in results_lists:
        n = len(results)
        seen_this_list: set = set()
        for pos, r in enumerate(results):
            k = key_of(r.url)
            borda = n - pos
            if k in scores:
                scores[k] += borda
                existing = by_key[k]
                for e in r.engines:
                    if e not in existing.engines:
                        existing.engines.append(e)
            else:
                scores[k] = borda
                by_key[k] = r
            if k not in seen_this_list:
                seen_this_list.add(k)
                if k in by_key and by_key[k] is not r:
                    by_key[k].instance_count += 1

    ranked = list(by_key.values())
    ranked.sort(key=lambda r: -scores[key_of(r.url)])
    return ranked
