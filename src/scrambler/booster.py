from urllib.parse import urlparse


def rank_by_engines(results: list, engine_ranking: list, consensus: bool) -> list:
    """Re-rank results using engine preference and/or consensus scoring.

    engine_ranking: ordered list of engine names — first = highest priority.
    consensus: if True, results found by more engines score higher.

    The two systems are independent and additive: either or both can be active.
    Stable sort, so equal-scored results keep their existing relative order.
    """
    if not engine_ranking and not consensus:
        return results

    n = len(engine_ranking)
    # Maps engine name → points (first engine = n pts, last = 1 pt)
    engine_pts: dict = {e.strip().lower(): n - i for i, e in enumerate(engine_ranking)}

    def _score(r) -> int:
        s = 0
        if engine_ranking:
            for eng in r.engines:
                s += engine_pts.get(eng.lower(), 0)
        if consensus:
            for eng in r.engines:
                # if ranking is on, use its point value (unranked engines = 1)
                # if ranking is off, every engine is worth 1 flat
                s += engine_pts.get(eng.lower(), 1) if engine_ranking else 1
            # also reward results found by multiple SearXNG instances
            s += getattr(r, 'instance_count', 1)
        return s

    return sorted(results, key=_score, reverse=True)


def boost_results(results: list, sources: list, cap: int = 1,
                  excluded_urls: set | None = None) -> list:
    """Move results whose hostname matches a priority source to the top.

    Sources are checked in list order — index 0 is highest priority.
    cap: maximum number of results to boost.
    excluded_urls: URLs that matched a source but failed a relevance check;
                   they stay in their original position and are not boosted.
    """
    if not sources or not results:
        return results

    excluded = excluded_urls or set()
    cap = max(1, cap)
    by_source: dict = {}   # source_rank -> [(orig_idx, result)]
    normal_idx: list = []

    for i, result in enumerate(results):
        if result.url in excluded:
            result.priority_source = None
            normal_idx.append((i, result))
            continue
        rank = _match_rank(result.url, sources)
        if rank is not None:
            result.priority_source = sources[rank]
            result.priority_rank = rank
            by_source.setdefault(rank, []).append((i, result))
        else:
            result.priority_source = None
            normal_idx.append((i, result))

    # Per source: take top `cap` by Scrambler's ranking; demote the rest
    promoted: list = []
    demoted: list = []
    for src_rank in sorted(by_source):
        candidates = sorted(by_source[src_rank], key=lambda x: x[0])
        promoted.extend(r for _, r in candidates[:cap])
        demoted.extend(candidates[cap:])

    # Rebuild normal section preserving original order
    all_normal = normal_idx + demoted
    all_normal.sort(key=lambda x: x[0])

    return promoted + [r for _, r in all_normal]


def demote_results(results: list, demote_domains: list) -> list:
    """Push results whose hostname matches a demoted domain to the end of the list."""
    if not demote_domains or not results:
        return results
    normal, demoted = [], []
    for r in results:
        if _match_rank(r.url, demote_domains) is not None:
            demoted.append(r)
        else:
            normal.append(r)
    return normal + demoted


def apply_domain_cap(results: list, cap: int) -> list:
    """Limit how many results per domain appear before the rest; excess moves to end."""
    if cap <= 0 or not results:
        return results
    counts: dict = {}
    normal, overflow = [], []
    for r in results:
        try:
            host = (urlparse(r.url).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
        except Exception:
            host = ""
        count = counts.get(host, 0)
        if count < cap:
            counts[host] = count + 1
            normal.append(r)
        else:
            overflow.append(r)
    return normal + overflow


def _match_rank(url: str, sources: list) -> int | None:
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    for i, source in enumerate(sources):
        s = source.lower().strip()
        if not s:
            continue
        if hostname == s or hostname.endswith("." + s):
            return i
    return None
