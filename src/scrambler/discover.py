import sys

import requests

SEARX_SPACE_API = "https://searx.space/data/instances.json"

_GRADE_ORDER = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}


def fetch_instances(min_grade: str | None = None) -> tuple[list, str | None]:
    """Return (instances, error). Always fetches direct — this is a public list, not a query."""
    try:
        resp = requests.get(SEARX_SPACE_API, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [], str(e)

    instances = []
    for url, info in data.get("instances", {}).items():
        if not url.startswith("https://"):
            continue

        http_info   = info.get("http") or {}
        tls_info    = info.get("tls") or {}
        html_info   = info.get("html") or {}
        uptime_info = info.get("uptime") or {}
        timing_info = info.get("timing") or {}
        network_info = info.get("network") or {}
        engines     = info.get("engines") or {}

        grade     = http_info.get("grade", "?")
        tls_grade = tls_info.get("grade", "?")
        csp_grade = html_info.get("grade", "?")

        uptime_day   = uptime_info.get("uptimeDay") or 0
        uptime_week  = uptime_info.get("uptimeWeek") or 0
        uptime_month = uptime_info.get("uptimeMonth") or 0

        # prefer search timing, fall back to initial load timing
        response_ms = None
        for key in ("search", "initial"):
            t = timing_info.get(key) or {}
            if t.get("success_percentage", 0) > 0:
                v = (t.get("all") or {}).get("value")
                if v:
                    response_ms = round(v * 1000)
                    break

        engine_names = sorted(
            name for name, e in engines.items()
            if (e.get("error_rate") or 0) < 100
        )

        instances.append({
            "url":           url.rstrip("/"),
            "grade":         grade,
            "tls_grade":     tls_grade,
            "csp_grade":     csp_grade,
            "uptime":        uptime_day,
            "uptime_week":   uptime_week,
            "uptime_month":  uptime_month,
            "response_ms":   response_ms,
            "network":       info.get("network_type", "normal"),
            "version":       (info.get("version") or ""),
            "analytics":     bool(info.get("analytics", False)),
            "contact":       bool(info.get("contact_url")),
            "ipv6":          bool(network_info.get("ipv6", False)),
            "dnssec":        int(network_info.get("dnssec") or 0),
            "engine_count":  len(engine_names),
            "engine_names":  engine_names,
        })

    if min_grade:
        cutoff = _GRADE_ORDER.get(min_grade.upper(), 7)
        instances = [i for i in instances if _GRADE_ORDER.get(i["grade"], 7) <= cutoff]

    instances.sort(key=lambda i: (_GRADE_ORDER.get(i["grade"], 7), -(i["uptime"] or 0)))
    return instances, None


def _score(inst: dict, historical: dict | None = None) -> float:
    s = 0.0
    http = {"A+": 40, "A": 30, "V": 35, "B": 10, "C": 5, "D": -10}
    tls  = {"A+": 20, "A": 15, "B": 5, "D": -8}
    csp  = {"A+": 15, "A": 10, "B": 5, "D": -5}
    s += http.get(inst.get("grade", ""), 0)
    s += tls.get(inst.get("tls_grade", ""), 0)
    s += csp.get(inst.get("csp_grade", ""), 0)
    if inst.get("contact"):    s += 20
    if inst.get("dnssec", 0):  s += 10
    if inst.get("ipv6"):       s += 5
    uptime = inst.get("uptime") or 0
    if uptime >= 99:   s += 10
    elif uptime >= 95: s += 5
    ms = inst.get("response_ms")
    if ms:             s += max(0, 10 - ms / 200)
    # engine depth bonus: 0.5 pts per working engine, capped at 30 engines (max +15)
    s += min(inst.get("engine_count", 0), 30) * 0.5

    # Historical stats from our own Tor sessions (more accurate than searx.space's ping)
    if historical:
        url = inst.get("url", "").rstrip("/")
        h = historical.get(url)
        if h:
            total = h.get("hits", 0) + h.get("total_failures", 0)
            if total >= 5:
                # Replace searx.space timing bonus with our observed Tor latency
                if ms:
                    s -= max(0, 10 - ms / 200)
                ema_ms = h.get("ema_seconds", 5.0) * 1000
                s += max(0, 15 - ema_ms / 400)  # up to +15 for <400 ms Tor latency

                failure_rate = h.get("total_failures", 0) / total
                if failure_rate > 0.4:
                    s -= 25  # chronically unreliable through Tor
                elif failure_rate < 0.05 and h.get("hits", 0) >= 20:
                    s += 10  # proven reliable with meaningful sample size
    return s


def autopick(n: int, min_engines: int = 0, historical_stats: dict | None = None) -> tuple[list[str], dict, str | None]:
    """Return (top_n_urls, engine_map, error_or_None) using privacy-first scoring."""
    instances, error = fetch_instances()
    if error:
        return [], {}, error
    CRITICAL_FAIL = {"F"}
    candidates = [
        i for i in instances
        if not i.get("analytics")
        and i.get("grade") not in CRITICAL_FAIL
        and i.get("tls_grade") not in CRITICAL_FAIL
        and i.get("csp_grade") not in CRITICAL_FAIL
        and i.get("engine_count", 0) >= min_engines
    ]
    if not candidates:
        return [], {}, "No qualifying instances found (all failed a critical check — try lowering the minimum engine count)"
    candidates.sort(key=lambda i: _score(i, historical_stats), reverse=True)
    top = candidates[:n]
    urls = [c["url"].rstrip("/") for c in top]
    engine_map = {c["url"].rstrip("/"): c.get("engine_names", []) for c in top}
    return urls, engine_map, None


def discover(min_grade: str | None = None) -> None:
    print("[Scrambler] Fetching instance list from searx.space ...", file=sys.stderr)
    instances, error = fetch_instances(min_grade)
    if error:
        print(f"[Scrambler] Error fetching instance list: {error}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'URL':<48} {'HTTP':<5} {'TLS':<5} {'Uptime':<7} {'ms':<6} {'Engines':<8} {'Network':<8} Version")
    print("─" * 100)
    for inst in instances:
        uptime_str = f"{inst['uptime']:.0f}%" if inst["uptime"] else "?"
        ms_str     = str(inst["response_ms"]) if inst["response_ms"] else "?"
        print(
            f"{inst['url']:<48} {inst['grade']:<5} {inst['tls_grade']:<5} {uptime_str:<7} {ms_str:<6} {inst['engine_count']:<8} {inst['network']:<8} {inst['version']}"
        )

    print(f"\n{len(instances)} instances listed.")
    print("\nAdding an instance to your list is an act of trust — that operator will see")
    print("your queries (as a Tor exit IP, not your real IP). Choose accordingly.\n")
    print("To add:  echo 'https://example.com' >> ~/.config/scrambler/instances.txt")
    print("Or use:  Settings page at http://127.0.0.1:7777/settings\n")
