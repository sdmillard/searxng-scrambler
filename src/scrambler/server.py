import concurrent.futures
import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, Response, redirect, render_template, request, stream_with_context, url_for

from .config import Config, DEFAULT_COLORS, load_config, load_instance_engines, load_themes, save_instance_engines, save_instances, save_prefs, save_themes, seed_themes, seed_search_profiles, load_ai_personalities, save_ai_personalities, AI_PERSONALITY_KEYS, load_map_data, save_map_data
from . import circuits
from .fetcher import check_tor, fetch
from .merger import merge_ranked
from .parser import parse
from .selector import InstanceSelector

_PRIVATE_HOST_RE = re.compile(
    r'^(localhost|127\.\d+\.\d+\.\d+|::1|0\.0\.0\.0|'
    r'10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|'
    r'172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)$'
)

# Keys in prefs that are scrambler-internal and must not be forwarded to SearXNG
_NON_SEARXNG_PREFS = {
    "multi_instance", "use_tor", "custom_dns", "weighted_selection",
    "result_cache", "circuit_warmup", "priority_sources",
    "engine_ranking", "consensus_ranking",
    "lexical_scoring", "semantic_reranking", "semantic_model", "semantic_cutoff",
    "priority_source_cap", "priority_source_cutoff", "appearance",
    "autopick_schedule", "autopick_interval", "autopick_count", "autopick_min_engines",
    "active_profile", "settings_customized", "default_category", "tab_categories",
    "engine_failover_limit", "failover_timeout", "failover_phase_timeout", "blocked_engines", "demote_domains", "demote_except_categories",
    "direct_engine_fallback", "brave_api_key",
    "domain_cap", "freshness_boost", "fuzzy_dedup", "reader_mode", "thumbnail_proxy", "wayback_links",
    "dearrow", "map_tile_provider", "map_tile_custom_url",
    "route_provider", "route_custom_url",
    "map_geolocation",
    "map_offline_method", "map_offline_behavior", "map_pmtiles_url",
    "route_transit_url", "transit_navitia_key",
    "ai_provider", "ai_api_key", "ai_base_url", "ai_model",
    "ai_max_results", "ai_routing", "ai_system_prompt",
    "ai_avatar", "ai_user_avatar",
    "ai_reranking", "ai_rerank_count", "ai_rerank_timing", "ai_rerank_on_ask",
    "ai_rerank_use_search_ai", "ai_rerank_provider", "ai_rerank_api_key",
    "ai_rerank_model", "ai_rerank_base_url", "ai_rerank_routing",
    "img_gen_provider", "img_gen_base_url", "img_gen_api_key",
    "img_gen_model", "img_gen_size", "img_gen_steps", "img_gen_cfg_scale",
    "img_gen_negative_prompt",
}


def _get_routing(prefs: dict) -> str:
    """Return the normalised routing string: 'tor' | 'tor_fallback' | 'direct'.
    Handles old boolean values from pre-migration saved preferences."""
    v = prefs.get("use_tor", "tor")
    if isinstance(v, bool):
        return "tor" if v else "direct"
    if v in ("tor", "tor_fallback", "direct"):
        return v
    return "tor"

def _get_map_routing(prefs: dict) -> str:
    """Return the normalised routing string for map API calls (geocoding + routing)."""
    v = prefs.get("map_routing", "tor")
    if v in ("tor", "tor_fallback", "direct"):
        return v
    return "tor"

def _get_tile_routing(prefs: dict) -> str:
    """Return the normalised routing string for map tile fetches."""
    v = prefs.get("map_tile_routing", "tor")
    if v in ("tor", "tor_fallback", "direct"):
        return v
    return "tor"


def _hostname(url: str) -> str:
    """Extract a short display label from an instance URL."""
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or url
        return h[:28]
    except Exception:
        return url[:28]


def _postprocess(results: list, query: str, cat: str, prefs: dict, boost: bool) -> list:
    """Apply the full ranking/filtering pipeline after raw results are collected."""
    from .booster import boost_results, rank_by_engines
    from .ranker import lexical_rerank, semantic_rerank
    if cat == "images":
        results = [r for r in results if r.img_src]
    elif cat == "videos":
        results = [r for r in results if r.duration]
    elif cat in _CATEGORY_NATIVE_ENGINES:
        native = _CATEGORY_NATIVE_ENGINES[cat]
        def _has_native(r, _n=native):
            return any(_engine_in_native(e, _n) for e in r.engines)
        if cat == "music":
            results = [r for r in results if _has_native(r) or (
                (r.result_type == "music" or r.duration) and not _result_is_foreign(r, cat))]
        elif cat == "files":
            results = [r for r in results if _has_native(r) or (
                r.result_type == "file" and not _result_is_foreign(r, cat))]
        elif cat == "news":
            results = [r for r in results if _has_native(r) or (
                r.date is not None and not _result_is_foreign(r, cat))]
        else:
            results = [r for r in results if _has_native(r)]

    engine_ranking_on = bool(prefs.get("engine_ranking", False))
    consensus = bool(prefs.get("consensus_ranking", False))
    if results and (engine_ranking_on or consensus):
        eng_list = [e.strip() for e in prefs.get("engines", "").split(",") if e.strip()] if engine_ranking_on else []
        results = rank_by_engines(results, eng_list, consensus)
    _is_media = cat == "map"
    if results and not _is_media and prefs.get("lexical_scoring"):
        results = lexical_rerank(results, query)
    if results and not _is_media and prefs.get("semantic_reranking"):
        results = semantic_rerank(
            results, query,
            prefs.get("semantic_model", "all-MiniLM-L6-v2"),
            cutoff=float(prefs.get("semantic_cutoff", 0.0)),
        )
    if results and not _is_media and prefs.get("freshness_boost"):
        from .ranker import freshness_rerank
        results = freshness_rerank(results, query)
    demote_domains = prefs.get("demote_domains") or []
    demote_except = {c.strip().lower() for c in (prefs.get("demote_except_categories") or [])}
    if demote_domains and results and cat.lower() not in demote_except:
        from .booster import demote_results
        results = demote_results(results, demote_domains)
    priority_sources = prefs.get("priority_sources") or []
    if boost and priority_sources and results:
        from .ranker import bm25_scores
        cap = max(1, int(prefs.get("priority_source_cap", 1)))
        ps_cutoff = float(prefs.get("priority_source_cutoff", 0.0))
        excluded_urls: set = set()
        if ps_cutoff > 0.0:
            scores = bm25_scores(results, query)
            excluded_urls = {r.url for r, s in zip(results, scores) if s < ps_cutoff}
        results = boost_results(results, priority_sources, cap=cap, excluded_urls=excluded_urls)
    domain_cap = int(prefs.get("domain_cap", 3))
    if domain_cap > 0 and results:
        from .booster import apply_domain_cap
        results = apply_domain_cap(results, domain_cap)
    return results

def _ai_rerank_results(results: list, query: str, config, _on_ask: bool = False) -> list:
    """Optionally rerank the top N results via the configured AI provider."""
    prefs = config.prefs
    if not results or not prefs.get("ai_reranking"):
        return results
    # "ask_only" timing: only rerank when triggered by Ask AI, not automatically
    if prefs.get("ai_rerank_timing") == "ask_only" and not _on_ask:
        return results

    use_search_ai = prefs.get("ai_rerank_use_search_ai", True)
    if use_search_ai:
        ai_provider = prefs.get("ai_provider", "").strip()
        ai_api_key  = prefs.get("ai_api_key", "").strip()
        ai_model    = prefs.get("ai_model", "").strip()
        ai_base_url = prefs.get("ai_base_url", "").strip().rstrip("/")
        ai_routing  = prefs.get("ai_routing", "direct")
    else:
        ai_provider = prefs.get("ai_rerank_provider", "").strip()
        ai_api_key  = prefs.get("ai_rerank_api_key", "").strip()
        ai_model    = prefs.get("ai_rerank_model", "").strip()
        ai_base_url = prefs.get("ai_rerank_base_url", "").strip().rstrip("/")
        ai_routing  = prefs.get("ai_rerank_routing", "direct")

    if not ai_provider or not ai_model:
        return results
    if ai_provider in ("anthropic", "openai_compat") and not ai_api_key:
        return results

    import re as _re2
    import requests as _req
    import json as _json
    from urllib.parse import urlparse as _urlparse

    count = max(1, min(int(prefs.get("ai_rerank_count", 20)), len(results)))
    to_rank = results[:count]
    rest    = results[count:]

    try:
        _host = (_urlparse(ai_base_url or "https://api.anthropic.com").hostname or "").lower()
        effective_routing = "direct" if _PRIVATE_HOST_RE.match(_host) else ai_routing

        proxies: dict = {}
        if effective_routing in ("tor", "tor_fallback"):
            try:
                cred = circuits.get_credential()
                p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                proxies = {"http": p, "https": p}
            except Exception:
                if effective_routing != "tor_fallback":
                    return results

        hdrs = {"Content-Type": "application/json", "User-Agent": "Scrambler/1.0"}

        if ai_provider == "rerank_api":
            # Dedicated reranking API (Cohere-compatible: OpenRouter, Cohere, Jina, etc.)
            # POST /v1/rerank with {model, query, documents:[...], top_n:N}
            base = (ai_base_url or "").rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            url = base + "/v1/rerank"
            h = {**hdrs}
            if ai_api_key:
                h["Authorization"] = f"Bearer {ai_api_key}"
            documents = []
            for r in to_rank:
                snippet = _re2.sub(r'<[^>]+>', '', (r.snippet or "")[:300].replace("\n", " "))
                documents.append(f"{r.title}\n{r.url}\n{snippet}")
            payload = {"model": ai_model, "query": query, "documents": documents, "top_n": count}
            resp = _req.post(url, json=payload, headers=h, proxies=proxies, timeout=30)
            resp.raise_for_status()
            ranked = resp.json().get("results", [])
            # results is [{index: 0, relevance_score: 0.9}, ...] already sorted best-first
            reranked, used = [], set()
            for item in ranked:
                i = int(item["index"])
                if 0 <= i < len(to_rank) and i not in used:
                    reranked.append(to_rank[i])
                    used.add(i)
            for i, r in enumerate(to_rank):
                if i not in used:
                    reranked.append(r)
            return reranked + rest

        # Chat-completion based reranking (Anthropic or OpenAI-compat)
        lines = []
        for i, r in enumerate(to_rank):
            snippet = _re2.sub(r'<[^>]+>', '', (r.snippet or "")[:150].replace("\n", " "))
            lines.append(f"{i+1}. {r.title}\n   {r.url}\n   {snippet}")

        prompt = (
            f'Rerank these search results for the query: "{query}"\n\n'
            + "\n\n".join(lines)
            + "\n\nOutput ONLY a JSON array of integers (1-based indices) in your preferred order "
            "of relevance. Example: [3,1,7,2]. Include every number exactly once."
        )

        if ai_provider == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            h = {**hdrs, "x-api-key": ai_api_key, "anthropic-version": "2023-06-01"}
            payload = {"model": ai_model, "max_tokens": 256,
                       "messages": [{"role": "user", "content": prompt}]}
            resp = _req.post(url, json=payload, headers=h, proxies=proxies, timeout=30)
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"]
        else:
            base = (ai_base_url or "https://api.openai.com").rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            url = base + "/v1/chat/completions"
            h = {**hdrs}
            if ai_api_key:
                h["Authorization"] = f"Bearer {ai_api_key}"
            payload = {"model": ai_model, "max_tokens": 256,
                       "messages": [{"role": "user", "content": prompt}]}
            resp = _req.post(url, json=payload, headers=h, proxies=proxies, timeout=30)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]

        match = _re2.search(r'\[[\d,\s]+\]', text)
        if match:
            order = _json.loads(match.group())
            reranked, used = [], set()
            for idx in order:
                i = int(idx) - 1
                if 0 <= i < len(to_rank) and i not in used:
                    reranked.append(to_rank[i])
                    used.add(i)
            for i, r in enumerate(to_rank):
                if i not in used:
                    reranked.append(r)
            return reranked + rest
    except Exception as _e:
        import sys
        print(f"[rerank] error: {_e}", file=sys.stderr)

    return results


# Per-stream accumulator so the stop-button finalize endpoint can run the final
# pipeline on whatever batches were collected before the client disconnected.
_stream_acc: dict = {}
_stream_acc_lock = threading.Lock()


def _failure_cooldown(result) -> int:
    """Return cooldown seconds appropriate for the type of fetch failure."""
    sc = result.status_code
    if sc == 429:
        return 30    # rate-limited: back off briefly
    if sc == 403:
        return 60    # blocked: likely Tor-specific, retry after a circuit change
    err = result.error or ""
    if "timeout" in err:
        return 30    # Tor circuit was slow: new circuit will be different
    if sc is not None and sc >= 500:
        return 60    # server error: give it a minute
    return 45        # other failure: short cooldown


def _blocked_engines_set(prefs: dict) -> set:
    """Return a lowercase set of engine names the user wants excluded from results."""
    raw = prefs.get("blocked_engines", "")
    if not raw:
        return set()
    import re
    return {e.strip().lower() for e in re.split(r"[\n,]+", raw) if e.strip()}


# ── Auto-pick scheduler ──────────────────────────────────────────────────────
_ap_lock   = threading.Lock()
_ap_wake   = threading.Event()
_ap_status: dict = {"last_run": None, "count": 0, "error": None}
_ap_last_search_run = 0.0   # monotonic timestamp of last search-triggered pick


def _ap_count(config: Config) -> int:
    return max(1, min(100, int(config.prefs.get("autopick_count", 5))))

def _ap_min_engines(config: Config) -> int:
    return max(0, int(config.prefs.get("autopick_min_engines", 5)))


def _run_autopick(config: Config, config_dir: Path, selector) -> None:
    if not _ap_lock.acquire(blocking=False):
        return
    try:
        from .discover import autopick
        historical = selector.dump_stats()
        urls, engine_map, error = autopick(
            _ap_count(config),
            min_engines=_ap_min_engines(config),
            historical_stats=historical,
        )
        if error:
            _ap_status.update(last_run=time.time(), count=0, error=error)
            print(f"[Scrambler] Auto-pick failed: {error}", file=sys.stderr)
        else:
            save_instances(config_dir / "instances.txt", urls)
            config.instances = urls
            selector.update_instances(urls)
            selector.load_stats(historical)  # restore EMA for any returning instances
            existing_map = load_instance_engines(config_dir / "instance_engines.json")
            existing_map.update(engine_map)
            save_instance_engines(config_dir / "instance_engines.json", existing_map)
            selector.set_engine_map(existing_map)
            threading.Thread(
                target=_refresh_engine_native_categories,
                args=(urls, _get_routing(config.prefs), config.tor_port),
                daemon=True,
                name="engine-cat-refresh",
            ).start()
            _ap_status.update(last_run=time.time(), count=len(urls), error=None)
            _persist_instance_stats(config_dir, selector)
            print(f"[Scrambler] Auto-picked {len(urls)} instances", file=sys.stderr)
    finally:
        _ap_lock.release()


def _start_ap_scheduler(config: Config, config_dir: Path, selector) -> None:
    def _loop() -> None:
        global _ap_last_search_run
        if config.prefs.get("autopick_schedule") == "boot":
            _run_autopick(config, config_dir, selector)
        while True:
            schedule = config.prefs.get("autopick_schedule", "never")
            sleep_s = (
                max(1, int(config.prefs.get("autopick_interval", 60))) * 60
                if schedule == "interval"
                else 3600
            )
            _ap_wake.wait(timeout=sleep_s)
            _ap_wake.clear()
            if config.prefs.get("autopick_schedule") == "interval":
                _run_autopick(config, config_dir, selector)

    threading.Thread(target=_loop, daemon=True, name="ap-scheduler").start()

# ── Query cache ──────────────────────────────────────────────────────────────
_cache: dict = {}
_CACHE_MAX = 100

_TEMPORAL_WORDS = frozenset({
    'today', 'yesterday', 'tonight', 'now', 'breaking', 'live',
    'latest', 'recent', 'new', 'current', 'update', 'updates', 'just',
})
_YEAR_RE = re.compile(r'\b20[12]\d\b')
_STABLE_WORDS = frozenset({'how', 'what', 'define', 'definition', 'tutorial',
                            'guide', 'documentation', 'wiki', 'history'})


def _query_ttl(query: str, cat: str) -> float:
    """Return cache TTL in seconds based on query and category."""
    if (cat or "").lower() == "news":
        return 30.0
    q_lower = query.lower()
    words = set(re.findall(r'\b\w+\b', q_lower))
    if words & _TEMPORAL_WORDS or _YEAR_RE.search(q_lower):
        return 30.0
    if words & _STABLE_WORDS:
        return 300.0
    return 120.0


def _cache_key(query: str, pageno: str, cat: str | None, prefs: dict, time_range: str = "") -> tuple:
    return (
        query.lower().strip(), pageno, cat or "",
        prefs.get("language", ""), prefs.get("safesearch", ""),
        prefs.get("categories", ""), prefs.get("engines", ""),
        time_range,
    )


_HISTORY_MAX = 500


def _load_history(config_dir: Path) -> list:
    p = config_dir / "search_history.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _append_history(config_dir: Path, query: str, cat: str) -> None:
    entries = _load_history(config_dir)
    entry = {"query": query, "cat": cat or "all", "ts": int(time.time())}
    if entries and entries[-1]["query"] == query and entries[-1]["cat"] == entry["cat"]:
        return
    entries.append(entry)
    if len(entries) > _HISTORY_MAX:
        entries = entries[-_HISTORY_MAX:]
    (config_dir / "search_history.json").write_text(json.dumps(entries) + "\n")


def _load_saved_results(config_dir: Path) -> list:
    p = config_dir / "saved_results.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save_saved_results(config_dir: Path, results: list) -> None:
    (config_dir / "saved_results.json").write_text(json.dumps(results, indent=2) + "\n")


def _persist_instance_stats(config_dir: Path, selector) -> None:
    try:
        data = selector.dump_stats()
        (config_dir / "instance_stats.json").write_text(json.dumps(data, indent=2) + "\n")
    except Exception:
        pass

def _cache_get(key: tuple):
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < entry[2]:
        return entry[1]
    _cache.pop(key, None)
    return None

def _cache_put(key: tuple, value, ttl: float = 120.0) -> None:
    if len(_cache) >= _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest)
    _cache[key] = (time.monotonic(), value, ttl)

logging.getLogger("werkzeug").setLevel(logging.ERROR)

_VALID_CATS = {"all", "general", "news", "images", "videos", "map", "music", "science", "it", "files", "social media"}
_DEFAULT_TAB_CATS = ["all", "general", "news", "images", "videos", "map", "music", "science", "it", "files", "social media"]

def _parse_tab_categories(raw: str) -> list:
    seen = set()
    result = []
    for v in raw.split(","):
        v = v.strip()
        if v in _VALID_CATS and v not in seen:
            seen.add(v)
            result.append(v)
    return result or list(_DEFAULT_TAB_CATS)


def create_app(config_dir: Path, no_tor: bool = False) -> Flask:
    import datetime as _dt
    app = Flask(__name__)

    @app.template_filter("datetimeformat")
    def _datetimeformat(ts):
        try:
            return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""

    config = load_config(config_dir)
    selector = InstanceSelector(config.instances, cooldown=config.unhealthy_cooldown,
                                weighted=config.prefs.get("weighted_selection", True))
    selector.set_engine_map(load_instance_engines(config_dir / "instance_engines.json"))
    seed_themes(config_dir / "themes.json")
    seed_search_profiles(config_dir / "search_profiles.json")

    # Restore per-instance health stats from previous session
    _stats_file = config_dir / "instance_stats.json"
    if _stats_file.exists():
        try:
            selector.load_stats(json.loads(_stats_file.read_text()))
        except Exception:
            pass

    # Periodically flush instance stats to disk so autopick can use them
    def _stats_flush_loop():
        while True:
            time.sleep(300)
            _persist_instance_stats(config_dir, selector)

    threading.Thread(target=_stats_flush_loop, daemon=True, name="stats-flush").start()

    _start_ap_scheduler(config, config_dir, selector)

    if config.instances:
        threading.Thread(
            target=_refresh_engine_native_categories,
            args=(list(config.instances), _get_routing(config.prefs), config.tor_port),
            daemon=True,
            name="engine-cat-refresh",
        ).start()

    _cw = int(config.prefs.get("circuit_warmup", 0))
    if _cw > 0 and _get_routing(config.prefs) != "direct":
        circuits.start(config.tor_port, pool_size=_cw)

    # CLI --no-tor overrides saved prefs
    if no_tor:
        config.prefs["use_tor"] = "direct"

    routing = _get_routing(config.prefs)
    if routing in ("tor", "tor_fallback") and not check_tor(config.tor_port):
        print(
            "\n[Scrambler] ERROR: Tor is not running.\n"
            f"  Expected SOCKS5 proxy at 127.0.0.1:{config.tor_port}\n"
            "\n"
            "  Install Tor:  https://www.torproject.org/download/\n"
            "  Debian/Ubuntu: sudo apt install tor && sudo systemctl start tor\n"
            "  macOS:         brew install tor && brew services start tor\n"
            "\n"
            "  Run without Tor via Settings or with --no-tor (your real IP will be\n"
            "  visible to every instance you query).\n",
            file=sys.stderr,
        )
        sys.exit(1)

    if routing == "direct":
        custom = config.prefs.get("custom_dns", "")
        dns_label = f"custom DNS {custom}" if custom else "system DNS"
        print(
            f"[Scrambler] WARNING: Tor off — using {dns_label}. Your real IP is visible to instances.",
            file=sys.stderr,
        )

    app.config.update(
        SCRAMBLER_CONFIG=config,
        SCRAMBLER_SELECTOR=selector,
        SCRAMBLER_CONFIG_DIR=config_dir,
        SCRAMBLER_NO_TOR=no_tor,
    )

    @app.context_processor
    def inject_appearance():
        from urllib.parse import quote
        app_prefs = config.prefs.get("appearance", {})
        favicon = app_prefs.get("favicon", "🔍")
        if favicon.startswith(("http://", "https://", "/")):
            favicon_href = favicon
        else:
            svg = f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>{quote(favicon)}</text></svg>"
            favicon_href = f"data:image/svg+xml,{svg}"
        return {
            "appearance": {**app_prefs, "favicon_href": favicon_href},
            "request_timeout": config.request_timeout,
        }

    @app.route("/")
    def index():
        return render_template("index.html", prefs=config.prefs)

    @app.route("/api/autopick/status")
    def api_autopick_status():
        from flask import jsonify
        return jsonify(_ap_status)

    @app.route("/history")
    def history_page():
        entries = list(reversed(_load_history(config_dir)))
        return render_template("history.html", entries=entries, prefs=config.prefs)

    @app.route("/api/history", methods=["GET"])
    def api_history_get():
        return jsonify(_load_history(config_dir))

    @app.route("/api/history", methods=["DELETE"])
    def api_history_delete():
        (config_dir / "search_history.json").write_text("[]\n")
        return jsonify({"ok": True})

    @app.route("/api/history/<int:idx>", methods=["DELETE"])
    def api_history_delete_one(idx):
        entries = _load_history(config_dir)
        if 0 <= idx < len(entries):
            entries.pop(idx)
            (config_dir / "search_history.json").write_text(json.dumps(entries) + "\n")
        return jsonify({"ok": True})

    @app.route("/saved")
    def saved_page():
        results = list(reversed(_load_saved_results(config_dir)))
        return render_template("saved.html", results=results, prefs=config.prefs)

    @app.route("/api/saved-results", methods=["GET"])
    def api_saved_results_get():
        return jsonify(_load_saved_results(config_dir))

    @app.route("/api/saved-results", methods=["POST"])
    def api_saved_results_post():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url required"}), 400
        results = _load_saved_results(config_dir)
        if any(r["url"] == url for r in results):
            return jsonify({"ok": True, "already": True})
        results.append({
            "url": url,
            "title": (data.get("title") or "").strip(),
            "snippet": (data.get("snippet") or "").strip()[:300],
            "saved_at": int(time.time()),
        })
        _save_saved_results(config_dir, results)
        return jsonify({"ok": True})

    @app.route("/api/saved-results", methods=["DELETE"])
    def api_saved_results_delete_by_url():
        url = (request.args.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url required"}), 400
        results = [r for r in _load_saved_results(config_dir) if r["url"] != url]
        _save_saved_results(config_dir, results)
        return jsonify({"ok": True})

    @app.route("/api/instance-stats")
    def api_instance_stats():
        from flask import jsonify
        return jsonify(selector.get_stats())

    @app.route("/search")
    def search():
        global _ap_last_search_run
        query = request.args.get("q", "").strip()
        if not query:
            return redirect(url_for("index"))
        pageno = request.args.get("pageno", "1")
        cat = request.args.get("cat", "").strip()
        if not cat:
            cat = config.prefs.get("default_category", "all")
        time_range = request.args.get("time_range", "").strip()
        boost = request.args.get("boost", "1") != "0"
        _append_history(config_dir, query, cat)
        # per-search auto-pick (non-blocking, throttled to at most once per 5 min)
        if config.prefs.get("autopick_schedule") == "search":
            now = time.monotonic()
            if now - _ap_last_search_run >= 300:
                _ap_last_search_run = now
                threading.Thread(
                    target=_run_autopick, args=(config, config_dir, selector),
                    daemon=True
                ).start()

        langs = [l.strip() for l in config.prefs.get("language", "en-US").split(",") if l.strip()]
        multi = max(1, int(config.prefs.get("multi_instance", 1)))
        _is_media = cat == "map"
        _app = config.prefs.get("appearance", {})
        _sr  = config.prefs.get("streaming_results", "off")   # "off" | "live"
        _li  = _app.get("loading_indicator", "bar")   # "bar"|"console"|"none"|"tree"
        _use_sse = _sr == "live" or (_sr == "off" and _li in ("console", "tracker"))
        if _use_sse and len(langs) == 1 and multi > 1 and not _is_media:
            if _sr == "live":
                _stream_mode = {"tree": "live", "console": "live_console"}.get(_li, "live_bare")
            else:
                _stream_mode = _li  # "console" or "tracker"
            return render_template(
                "results.html",
                query=query, results=[], error=None, prefs=config.prefs,
                pageno=int(pageno), active_cat=cat, boost=boost,
                time_range=time_range,
                has_priority_sources=bool(config.prefs.get("priority_sources")),
                active_profile=config.prefs.get("active_profile", ""),
                settings_customized=config.prefs.get("settings_customized", False),
                streaming=True, stream_mode=_stream_mode,
            )

        results, error = _do_search(query, config, selector, pageno, cat or None, time_range)
        results = _postprocess(results, query, cat, config.prefs, boost)
        results = _ai_rerank_results(results, query, config)

        # Pre-warm the cache for other visible tabs so clicking them is instant.
        # Only runs when caching is on and we got real results (not an error page).
        if results and config.prefs.get("result_cache", True):
            _prefetch_tab_categories(query, pageno, cat, config, selector)

        return render_template(
            "results.html",
            query=query, results=results, error=error, prefs=config.prefs,
            pageno=int(pageno), active_cat=cat, boost=boost,
            time_range=time_range,
            has_priority_sources=bool(config.prefs.get("priority_sources")),
            active_profile=config.prefs.get("active_profile", ""),
            settings_customized=config.prefs.get("settings_customized", False),
        )

    @app.route("/api/search/stream")
    def search_stream():
        query = request.args.get("q", "").strip()
        if not query:
            err_payload = (
                f"data: {json.dumps({'type': 'error', 'message': 'No query'})}\n\n"
                f"data: {json.dumps({'type': 'done'})}\n\n"
            )
            return Response(err_payload, mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        pageno = request.args.get("pageno", "1")
        cat = request.args.get("cat", "").strip() or config.prefs.get("default_category", "all")
        boost = request.args.get("boost", "1") != "0"
        time_range = request.args.get("time_range", "").strip()
        _sid = request.args.get("_sid", "").strip()
        prefs_snap = dict(config.prefs)

        def generate():
            multi = max(1, int(prefs_snap.get("multi_instance", 1)))
            use_tor = _get_routing(prefs_snap)
            custom_dns = prefs_snap.get("custom_dns", "")
            params = _build_params(query, pageno, prefs_snap, cat or None, time_range)

            # Engine list from params (may be empty if using instance defaults)
            engines_param = params.get("engines", "")
            known_engines: list = [e.strip() for e in engines_param.split(",") if e.strip()] if engines_param else []
            seen_engines: set = set()

            seen_urls: set = set()
            all_batches: list = []
            tried_instances: set = set()

            known_engines_lower = {e.lower() for e in known_engines}

            blocked_engines = _blocked_engines_set(prefs_snap)

            def _handle_batch(event):
                """Shared batch-processing logic used for both main and failover phases."""
                inst, batch, batch_engines = event["instance"], event["results"], event["engines"]
                new_results = [r for r in batch if r.url not in seen_urls]
                if cat == "images":
                    new_results = [r for r in new_results if r.img_src]
                elif cat == "videos":
                    new_results = [r for r in new_results if r.duration]
                elif cat in _CATEGORY_NATIVE_ENGINES:
                    native = _CATEGORY_NATIVE_ENGINES[cat]
                    def _has_native_b(r, _n=native):
                        return any(_engine_in_native(e, _n) for e in r.engines)
                    if cat == "music":
                        new_results = [r for r in new_results if _has_native_b(r) or (
                            (r.result_type == "music" or r.duration) and not _result_is_foreign(r, cat))]
                    elif cat == "files":
                        new_results = [r for r in new_results if _has_native_b(r) or (
                            r.result_type == "file" and not _result_is_foreign(r, cat))]
                    elif cat == "news":
                        new_results = [r for r in new_results if _has_native_b(r) or (
                            r.date is not None and not _result_is_foreign(r, cat))]
                    else:
                        new_results = [r for r in new_results if _has_native_b(r)]
                if blocked_engines:
                    new_results = [
                        r for r in new_results
                        if not all(e.lower() in blocked_engines
                                   for e in r.engines if e.lower() != "cached")
                    ]
                for r in new_results:
                    seen_urls.add(r.url)
                all_batches.append(new_results)
                for eng in batch_engines:
                    eng_l = eng.lower()
                    if eng_l == "cached":
                        continue
                    if eng_l not in seen_engines:
                        seen_engines.add(eng_l)
                        is_known = eng_l in known_engines_lower
                        if not is_known:
                            yield f"data: {json.dumps({'type': 'tree_add', 'engine': eng})}\n\n"
                        yield f"data: {json.dumps({'type': 'tree_update', 'engine': eng, 'status': 'ok'})}\n\n"
                if new_results:
                    html = render_template("_result_card.html",
                                           results=new_results,
                                           prefs=prefs_snap,
                                           active_cat=cat or "all")
                    yield f"data: {json.dumps({'type': 'batch', 'html': html})}\n\n"
                interim = merge_ranked(all_batches, normalize=prefs_snap.get("fuzzy_dedup", True))
                interim = _postprocess(interim, query, cat or "all", prefs_snap, boost)
                if prefs_snap.get("ai_rerank_timing") == "streaming":
                    interim = _ai_rerank_results(interim, query, config)
                if _sid:
                    with _stream_acc_lock:
                        _stream_acc[_sid] = list(all_batches)
                yield f"data: {json.dumps({'type': 'rerank', 'order': [r.url for r in interim]})}\n\n"
                ps_cards = [r for r in interim if r.priority_source]
                if ps_cards:
                    ps_html = render_template("_result_card.html", results=ps_cards,
                                              prefs=prefs_snap, active_cat=cat or "all")
                    yield f"data: {json.dumps({'type': 'card_replace', 'html': ps_html})}\n\n"

            def _log(tag, msg=''):
                return f"data: {json.dumps({'type': 'log', 'tag': tag, 'msg': msg})}\n\n"

            try:
                for event in _multi_search_streaming(multi, params, config, selector, use_tor, custom_dns,
                                                     tried_out=tried_instances):
                    etype = event["type"]

                    if etype == "init":
                        hosts = [_hostname(u) for u in event.get("instances", [])]
                        yield _log('→', ', '.join(hosts))
                        yield f"data: {json.dumps({'type': 'tree_init', 'engines': known_engines})}\n\n"

                    elif etype == "batch":
                        host = _hostname(event["instance"])
                        n = len(event.get("results", []))
                        engs = ', '.join(event.get("engines", [])[:5])
                        t = event.get("elapsed", 0)
                        yield _log('✓', f'{host} — {n} results ({engs}) in {t:.1f}s')
                        yield from _handle_batch(event)

                    elif etype == "fail":
                        t = event.get("elapsed", 0)
                        yield _log('✗', f'{_hostname(event["instance"])} — failed ({t:.1f}s)')

                    elif etype == "timeout":
                        yield _log('✗', f'{_hostname(event["instance"])} — timeout')

                    elif etype == "init_extra":
                        yield _log('→', f'fallback: {_hostname(event["instance"])}')

            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            if not all_batches:
                if prefs_snap.get("direct_engine_fallback") and known_engines:
                    yield _log('→', f'direct: {", ".join(known_engines[:5])}')
                    from .direct_engines import search_direct_engines as _sde
                    _dir0 = _sde(known_engines, query, pageno, config, use_tor)
                    if _dir0:
                        yield from _handle_batch({
                            "type": "batch", "instance": "__direct__",
                            "results": _dir0,
                            "engines": list({e for r in _dir0 for e in r.engines}),
                        })
                if not all_batches:
                    msg = ("No instances configured." if not config.instances
                           else "All instances failed or timed out.")
                    yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

            # ── Early AI rerank (only if we have enough results) ─────────────
            # Skip if the initial parallel search returned too few results and
            # engine failover is going to run — firing on e.g. 9 results while
            # failover will add 300 more just creates noise and then the failover
            # batch reranks scramble the order anyway.
            _early_count = sum(len(b) for b in all_batches)
            _failover_limit = int(prefs_snap.get("engine_failover_limit", 3))
            _missing_engines = [e for e in known_engines if e.lower() not in seen_engines] if known_engines else []
            _will_failover = bool(_missing_engines and _failover_limit > 0)
            _ai_rerank_threshold = max(10, int(prefs_snap.get("ai_rerank_count", 20)) // 2)
            _fire_early = _early_count >= _ai_rerank_threshold or not _will_failover

            def _do_ai_rerank_event(batches):
                try:
                    _m = merge_ranked(batches, normalize=prefs_snap.get("fuzzy_dedup", True))
                    _m = _postprocess(_m, query, cat or "all", prefs_snap, boost)
                    _ai_did = prefs_snap.get("ai_reranking") and bool(prefs_snap.get("ai_rerank_provider") or prefs_snap.get("ai_rerank_use_search_ai", True))
                    if _ai_did:
                        yield _log('AI', f'reranking top {len(_m)}…')
                    _m = _ai_rerank_results(_m, query, config)
                    yield f"data: {json.dumps({'type': 'rerank', 'order': [r.url for r in _m], 'ai_reranked': _ai_did})}\n\n"
                    _ps = [r for r in _m if r.priority_source]
                    if _ps:
                        _ps_html = render_template("_result_card.html", results=_ps,
                                                   prefs=prefs_snap, active_cat=cat or "all")
                        yield f"data: {json.dumps({'type': 'card_replace', 'html': _ps_html})}\n\n"
                except Exception as _e:
                    import sys, traceback
                    print(f"[ai-rerank] error: {_e!r}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)

            if _fire_early:
                yield from _do_ai_rerank_event(all_batches)

            # ── Engine failover: stream more results for missing engines ──────
            if _will_failover:
                yield _log('→', f'failover: {", ".join(_missing_engines[:5])}')
                try:
                    for event in _retry_engines_streaming(_missing_engines, query, pageno, config, selector,
                                                          tried_instances, use_tor, custom_dns):
                        if event["type"] == "batch":
                            host = _hostname(event["instance"])
                            n = len(event.get("results", []))
                            engs = ', '.join(event.get("engines", [])[:5])
                            yield _log('✓', f'{host} — {n} failover ({engs})')
                            yield from _handle_batch(event)
                except Exception:
                    pass

            # ── Direct engine fallback for still-missing engines ──────────────
            # Must run BEFORE the post-failover AI rerank so its results are
            # included in the final ordering pass, not appended after.
            if prefs_snap.get("direct_engine_fallback") and known_engines:
                _still_missing = [e for e in known_engines if e.lower() not in seen_engines]
                if _still_missing:
                    yield _log('→', f'direct: {", ".join(_still_missing[:5])}')
                    from .direct_engines import search_direct_engines as _sde2
                    _dir2 = _sde2(_still_missing, query, pageno, config, use_tor)
                    if _dir2:
                        yield from _handle_batch({
                            "type": "batch", "instance": "__direct__",
                            "results": _dir2,
                            "engines": list({e for r in _dir2 for e in r.engines}),
                        })

            # ── Post-failover AI rerank (when early fire was skipped) ─────────
            if not _fire_early:
                yield from _do_ai_rerank_event(all_batches)

            # Mark known engines that never returned results as failed
            for eng in known_engines:
                if eng.lower() not in seen_engines:
                    yield f"data: {json.dumps({'type': 'tree_update', 'engine': eng, 'status': 'fail'})}\n\n"

            # ── Final cache + cleanup ─────────────────────────────────────────
            try:
                if _sid:
                    with _stream_acc_lock:
                        _stream_acc.pop(_sid, None)
                if prefs_snap.get("result_cache", True):
                    final = merge_ranked(all_batches, normalize=prefs_snap.get("fuzzy_dedup", True))
                    final = _postprocess(final, query, cat or "all", prefs_snap, boost)
                    key = _cache_key(query, pageno, cat or None, prefs_snap, time_range)
                    _cache_put(key, final, _query_ttl(query, cat or ""))
                    _prefetch_tab_categories(query, pageno, cat or "general", config, selector)
            except Exception as _e:
                import sys
                print(f"[cache] error: {_e!r}", file=sys.stderr)

            _total = sum(len(b) for b in all_batches)
            yield _log('done', f'{_total} result{"s" if _total != 1 else ""}')
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return Response(stream_with_context(generate()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/debug/rerank")
    def debug_rerank():
        from flask import jsonify
        import requests as _req, json as _json, re as _re2
        from urllib.parse import urlparse as _urlparse
        prefs = config.prefs
        provider = prefs.get("ai_rerank_provider", "")
        use_search = prefs.get("ai_rerank_use_search_ai", True)
        if use_search:
            provider = prefs.get("ai_provider", "")
            model = prefs.get("ai_model", "")
            key = prefs.get("ai_api_key", "")
            base_url = prefs.get("ai_base_url", "")
        else:
            model = prefs.get("ai_rerank_model", "")
            key = prefs.get("ai_rerank_api_key", "")
            base_url = prefs.get("ai_rerank_base_url", "")
        info = {
            "ai_reranking": prefs.get("ai_reranking"),
            "provider": provider,
            "model": model,
            "has_key": bool(key),
            "base_url": base_url,
            "use_search_ai": use_search,
        }
        if not prefs.get("ai_reranking"):
            return jsonify({**info, "status": "disabled"})
        if not provider or not model:
            return jsonify({**info, "status": "missing provider or model"})
        if provider in ("anthropic", "openai_compat", "rerank_api") and not key:
            return jsonify({**info, "status": "missing api key"})
        try:
            hdrs = {"Content-Type": "application/json", "User-Agent": "Scrambler/1.0"}
            if provider == "rerank_api":
                base = (base_url or "").rstrip("/")
                if base.endswith("/v1"):
                    base = base[:-3]
                url = base + "/v1/rerank"
                h = {**hdrs, "Authorization": f"Bearer {key}"}
                payload = {"model": model, "query": "test query", "documents": ["first result", "second result"], "top_n": 2}
                resp = _req.post(url, json=payload, headers=h, timeout=15)
                return jsonify({**info, "status": "ok" if resp.ok else "error", "http": resp.status_code, "body": resp.json()})
            else:
                return jsonify({**info, "status": "chat-based provider — use a search to test"})
        except Exception as e:
            return jsonify({**info, "status": "exception", "error": str(e)})

    @app.route("/api/search/finalize", methods=["POST"])
    def search_finalize():
        from flask import jsonify
        data = request.get_json(force=True) or {}
        sid = (data.get("sid") or "").strip()
        if not sid:
            return jsonify({"order": []}), 400

        with _stream_acc_lock:
            batches = _stream_acc.pop(sid, None)

        if not batches:
            return jsonify({"order": []})

        q      = (data.get("q") or "").strip()
        c      = (data.get("cat") or "all").strip()
        pg     = str(data.get("pageno", "1"))
        boost  = bool(data.get("boost", True))
        prefs  = dict(config.prefs)

        merged = merge_ranked(batches, normalize=prefs.get("fuzzy_dedup", True))
        merged = _postprocess(merged, q, c, prefs, boost)
        merged = _ai_rerank_results(merged, q, config, _on_ask=True)

        if prefs.get("result_cache", True):
            key = _cache_key(q, pg, c or None, prefs)
            _cache_put(key, merged, _query_ttl(q, c or ""))

        return jsonify({"order": [r.url for r in merged]})

    @app.route("/goto")
    def goto():
        import requests as _req
        from urllib.parse import urlparse
        from .reader import extract_content
        from bs4 import BeautifulSoup

        _READER_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

        url = request.args.get("url", "").strip()
        if not url:
            return render_template("reader.html", url="", title="Bad request",
                                   content=None, error="No URL provided.")

        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            return render_template("reader.html", url=url, title="Bad request",
                                   content=None, error="Only http/https URLs are supported.")

        host = (parsed_url.hostname or "").lower()
        if _PRIVATE_HOST_RE.match(host):
            return render_template("reader.html", url=url, title="Blocked",
                                   content=None, error="Private/local addresses are not allowed.")

        via = request.args.get("via", "")
        if via == "jina":
            import markdown as _md
            jina_routing = config.prefs.get("jina_routing", "direct")
            _jina_key = config.prefs.get("jina_api_key", "").strip()
            _jina_hdrs = {
                "X-Return-Format": "markdown",
                "User-Agent": "Mozilla/5.0 (compatible; Scrambler/1.0)",
            }
            if _jina_key:
                _jina_hdrs["Authorization"] = f"Bearer {_jina_key}"

            _jina_ft = int(config.prefs.get('failover_timeout', 12))

            def _do_jina_get(proxies, timeout=20):
                return _req.get(
                    "https://r.jina.ai/" + url,
                    proxies=proxies,
                    headers=_jina_hdrs,
                    timeout=timeout,
                    allow_redirects=True,
                )

            try:
                j_proxies = {}
                if jina_routing in ("tor", "tor_fallback"):
                    try:
                        cred = circuits.get_credential()
                        p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                        j_proxies = {"http": p, "https": p}
                    except Exception:
                        if jina_routing == "tor":
                            return render_template("reader.html", url=url, title=url, content=None,
                                                   error="Tor unavailable for Jina request.")

                j_resp = _do_jina_get(j_proxies, timeout=_jina_ft if jina_routing == "tor_fallback" else 20)

                # tor_fallback: retry direct if Tor is blocked by Jina
                if not j_resp.ok and j_proxies and jina_routing == "tor_fallback":
                    j_resp = _do_jina_get({}, timeout=20)

                if not j_resp.ok:
                    _jerr = "Jina requires an API key — add one in Settings → Routing." if j_resp.status_code == 403 else f"Jina returned HTTP {j_resp.status_code}."
                    return render_template("reader.html", url=url, title=url, content=None,
                                           error=_jerr)
                md_text = j_resp.text
                title = url
                for line in md_text.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        title = stripped[2:].strip()
                        break
                content_html = _md.markdown(md_text, extensions=["fenced_code", "tables"])
                return render_template("reader.html", url=url, title=title, content=content_html, error=None)
            except _req.exceptions.Timeout:
                return render_template("reader.html", url=url, title=url, content=None,
                                       error="Jina request timed out (20 s).")
            except Exception as e:
                return render_template("reader.html", url=url, title=url, content=None, error=str(e))

        if _get_routing(config.prefs) == "direct":
            return render_template("reader.html", url=url, title=url,
                                   content=None,
                                   error="Reader mode requires Tor routing to protect your IP. "
                                         "Enable Tor in Settings, then try again.")

        cred = circuits.get_credential()
        proxies = {
            "http":  f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}",
            "https": f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "DNT": "1",
        }

        try:
            resp = _req.get(url, proxies=proxies, headers=headers,
                            timeout=20, allow_redirects=True, stream=True)

            if resp.status_code == 403:
                return render_template("reader.html", url=url, title=url, content=None,
                                       error="This site blocks Tor exit nodes (403 Forbidden). "
                                             "Use the direct link to open it normally.")
            if resp.status_code == 404:
                return render_template("reader.html", url=url, title=url, content=None,
                                       error="Page not found (404).")
            if resp.status_code == 429:
                return render_template("reader.html", url=url, title=url, content=None,
                                       error="Rate limited (429). The site is throttling Tor exit nodes.")
            if not resp.ok:
                return render_template("reader.html", url=url, title=url, content=None,
                                       error=f"The site returned HTTP {resp.status_code}. "
                                             "It may block Tor — use the direct link instead.")

            ctype = resp.headers.get("Content-Type", "")
            if "text/html" not in ctype and "text/plain" not in ctype:
                return render_template("reader.html", url=url, title=url,
                                       content=None,
                                       error=f"Cannot render content type: {ctype.split(';')[0].strip()}")

            raw = b""
            for chunk in resp.iter_content(chunk_size=65536):
                raw += chunk
                if len(raw) > _READER_MAX_BYTES:
                    resp.close()
                    return render_template("reader.html", url=url, title=url,
                                           content=None, error="Page too large (> 2 MB).")

            text = raw.decode(resp.apparent_encoding or "utf-8", errors="replace")
            soup = BeautifulSoup(text, "lxml")
            title, safe_html = extract_content(soup, url)
            return render_template("reader.html", url=url, title=title,
                                   content=safe_html, error=None)

        except _req.exceptions.Timeout:
            return render_template("reader.html", url=url, title=url, content=None,
                                   error="Request timed out (20 s). The page may be slow or blocking Tor.")
        except _req.exceptions.ConnectionError:
            return render_template("reader.html", url=url, title=url, content=None,
                                   error="Connection failed. The site may be blocking Tor exit nodes "
                                         "or the address is unreachable.")
        except Exception as e:
            return render_template("reader.html", url=url, title=url, content=None,
                                   error=str(e))

    @app.route("/img")
    def img_proxy():
        import requests as _req
        from urllib.parse import urlparse
        from flask import make_response

        url = request.args.get("url", "").strip()
        if not url:
            return "", 400

        try:
            parsed_img = urlparse(url)
        except Exception:
            return "", 400

        if parsed_img.scheme not in ("http", "https"):
            return "", 400

        host = (parsed_img.hostname or "").lower()
        if _PRIVATE_HOST_RE.search(host):
            return "", 403

        mode = config.prefs.get("thumbnail_proxy", "direct")
        tor_proxies = {
            "http":  f"socks5h://127.0.0.1:{config.tor_port}",
            "https": f"socks5h://127.0.0.1:{config.tor_port}",
        }
        ua_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0",
            "Accept": "image/webp,image/*,*/*;q=0.8",
        }

        def _fetch(proxies, timeout):
            r = _req.get(url, proxies=proxies, headers=ua_headers, timeout=timeout, stream=True)
            return r

        _thumb_ft = int(config.prefs.get('failover_timeout', 12))
        try:
            if mode == "tor":
                resp = _fetch(tor_proxies, 20)
            elif mode == "tor_fallback":
                try:
                    resp = _fetch(tor_proxies, _thumb_ft)
                    if not resp.ok:
                        raise Exception("non-ok")
                except Exception:
                    resp = _fetch({}, 8)
            else:  # "direct"
                resp = _fetch({}, 10)

            if not resp.ok:
                return "", resp.status_code
            ctype = resp.headers.get("Content-Type", "image/jpeg")
            if not ctype.startswith("image/"):
                return "", 415
            raw = b"".join(resp.iter_content(65536))[:512 * 1024]
            response = make_response(raw)
            response.headers["Content-Type"] = ctype
            response.headers["Cache-Control"] = "public, max-age=3600"
            return response
        except Exception:
            return "", 502

    @app.route("/backgrounds/<filename>")
    def serve_background(filename):
        import re
        from flask import send_from_directory, abort
        if not re.fullmatch(r"[a-f0-9]{16}\.(jpg|jpeg|png|gif|webp|mp4|webm|mov|ogg)", filename):
            abort(404)
        bg_dir = config_dir / "backgrounds"
        if not (bg_dir / filename).exists():
            abort(404)
        return send_from_directory(bg_dir, filename)

    @app.route("/api/upload-background", methods=["POST"])
    def api_upload_background():
        import hashlib, os
        from flask import jsonify
        _ALLOWED = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov", ".ogg"}
        _MAX = 200 * 1024 * 1024  # 200 MB — video needs room
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file provided"}), 400
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in _ALLOWED:
            return jsonify({"error": f"Unsupported type. Allowed: {', '.join(_ALLOWED)}"}), 400
        data = f.read(_MAX + 1)
        if len(data) > _MAX:
            return jsonify({"error": "File too large (max 20 MB)"}), 413
        bg_dir = config_dir / "backgrounds"
        bg_dir.mkdir(parents=True, exist_ok=True)
        filename = hashlib.sha1(data).hexdigest()[:16] + ext
        (bg_dir / filename).write_bytes(data)
        return jsonify({"url": f"/backgrounds/{filename}"})

    @app.route("/opensearch.xml")
    def opensearch_xml():
        from flask import Response, request as _req
        base = _req.host_url.rstrip("/")
        title = config.prefs.get("appearance", {}).get("title", "Scrambler")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>{title}</ShortName>
  <Description>Privacy search — Tor-routed SearXNG</Description>
  <Url type="text/html" template="{base}/search?q={{searchTerms}}"/>
  <InputEncoding>UTF-8</InputEncoding>
  <Image width="16" height="16" type="image/svg+xml">{base}/favicon.ico</Image>
</OpenSearchDescription>"""
        return Response(xml.strip(), mimetype="application/opensearchdescription+xml")

    @app.route("/map-sw.js")
    def serve_map_sw():
        from flask import send_from_directory
        static_dir = Path(__file__).parent / "static"
        return send_from_directory(static_dir, "map-sw.js",
                                   mimetype="application/javascript",
                                   max_age=0)

    @app.route("/fonts/<filename>")
    def serve_font(filename):
        import re
        from flask import send_from_directory, abort
        if not re.fullmatch(r"[a-zA-Z0-9_\-]+\.woff2", filename):
            abort(404)
        fonts_dir = config_dir / "fonts"
        if not (fonts_dir / filename).exists():
            abort(404)
        return send_from_directory(fonts_dir, filename, mimetype="font/woff2",
                                   max_age=31536000)

    @app.route("/api/download-font", methods=["POST"])
    def api_download_font():
        from flask import jsonify
        import re, hashlib, urllib.request
        body = request.get_json(force=True) or {}
        fonts_url = (body.get("url") or "").strip()
        if not fonts_url.startswith("https://fonts.googleapis.com/"):
            return jsonify({"error": "Only Google Fonts URLs are supported"}), 400

        UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        fonts_dir = config_dir / "fonts"
        fonts_dir.mkdir(parents=True, exist_ok=True)

        try:
            req = urllib.request.Request(fonts_url, headers={"User-Agent": UA})
            css = urllib.request.urlopen(req, timeout=15).read().decode()
        except Exception as e:
            return jsonify({"error": f"Could not fetch font CSS: {e}"}), 502

        woff2_urls = re.findall(r"url\((https://fonts\.gstatic\.com/[^\)]+\.woff2)\)", css)
        if not woff2_urls:
            return jsonify({"error": "No woff2 URLs found in the font CSS"}), 400

        url_map = {}
        for url in dict.fromkeys(woff2_urls):  # deduplicate, preserve order
            fname = hashlib.sha1(url.encode()).hexdigest()[:16] + ".woff2"
            dest = fonts_dir / fname
            if not dest.exists():
                try:
                    r = urllib.request.urlopen(
                        urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15)
                    dest.write_bytes(r.read())
                except Exception as e:
                    return jsonify({"error": f"Failed to download {url}: {e}"}), 502
            url_map[url] = f"/fonts/{fname}"

        local_css = css
        for remote, local_path in url_map.items():
            local_css = local_css.replace(remote, local_path)

        face_blocks = re.findall(r"@font-face\s*\{[^}]+\}", local_css, re.DOTALL)
        font_face_css = "\n".join(face_blocks)

        family_match = re.search(r"font-family:\s*['\"]?([^;'\"]+)['\"]?", font_face_css)
        font_family = family_match.group(1).strip().strip("'\"") if family_match else ""

        return jsonify({"font_face_css": font_face_css, "font_family": font_family})

    @app.route("/cursors/<filename>")
    def serve_cursor(filename):
        import re
        from flask import send_from_directory, abort
        if not re.fullmatch(r"[a-f0-9]{16}\.(png|svg|cur|gif)", filename):
            abort(404)
        cursor_dir = config_dir / "cursors"
        if not (cursor_dir / filename).exists():
            abort(404)
        return send_from_directory(cursor_dir, filename)

    @app.route("/api/upload-cursor", methods=["POST"])
    def api_upload_cursor():
        import hashlib, os
        from flask import jsonify
        _ALLOWED = {".png", ".svg", ".cur", ".gif"}
        _MAX = 2 * 1024 * 1024  # 2 MB — cursors should be tiny
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file provided"}), 400
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in _ALLOWED:
            return jsonify({"error": f"Unsupported: {', '.join(_ALLOWED)}"}), 400
        data = f.read(_MAX + 1)
        if len(data) > _MAX:
            return jsonify({"error": "File too large (max 2 MB)"}), 413
        cursor_dir = config_dir / "cursors"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        fname = hashlib.sha1(data).hexdigest()[:16] + ext
        (cursor_dir / fname).write_bytes(data)
        return jsonify({"url": f"/cursors/{fname}"})

    @app.route("/icons/<filename>")
    def serve_icon(filename):
        import re
        from flask import send_from_directory, abort
        if not re.fullmatch(r"[a-f0-9]{16}\.(png|jpg|jpeg|gif|webp|svg)", filename):
            abort(404)
        icon_dir = config_dir / "icons"
        if not (icon_dir / filename).exists():
            abort(404)
        return send_from_directory(icon_dir, filename)

    @app.route("/api/upload-icon", methods=["POST"])
    def api_upload_icon():
        import hashlib, os
        from flask import jsonify
        _ALLOWED = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
        _MAX = 512 * 1024  # 512 KB — these are small status icons
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file provided"}), 400
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in _ALLOWED:
            return jsonify({"error": f"Unsupported: {', '.join(_ALLOWED)}"}), 400
        data = f.read(_MAX + 1)
        if len(data) > _MAX:
            return jsonify({"error": "File too large (max 512 KB)"}), 413
        icon_dir = config_dir / "icons"
        icon_dir.mkdir(parents=True, exist_ok=True)
        fname = hashlib.sha1(data).hexdigest()[:16] + ext
        (icon_dir / fname).write_bytes(data)
        return jsonify({"url": f"/icons/{fname}"})

    @app.route("/audio/<filename>")
    def serve_audio(filename):
        import re
        from flask import send_from_directory, abort
        if not re.fullmatch(r"[a-f0-9]{16}\.(mp3|ogg|wav|flac|m4a|aac|opus|webm)", filename):
            abort(404)
        audio_dir = config_dir / "audio"
        if not (audio_dir / filename).exists():
            abort(404)
        return send_from_directory(audio_dir, filename)

    @app.route("/api/upload-audio", methods=["POST"])
    def api_upload_audio():
        import hashlib, os
        from flask import jsonify
        _ALLOWED = {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".opus", ".webm"}
        _MAX = 50 * 1024 * 1024
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file provided"}), 400
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in _ALLOWED:
            return jsonify({"error": f"Unsupported: {', '.join(_ALLOWED)}"}), 400
        data = f.read(_MAX + 1)
        if len(data) > _MAX:
            return jsonify({"error": "File too large (max 50 MB)"}), 413
        audio_dir = config_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        fname = hashlib.sha1(data).hexdigest()[:16] + ext
        (audio_dir / fname).write_bytes(data)
        return jsonify({"url": f"/audio/{fname}"})

    @app.route("/api/themes", methods=["GET", "POST"])
    def api_themes():
        from flask import jsonify
        themes_path = config_dir / "themes.json"
        if request.method == "POST":
            body = request.get_json(force=True) or {}
            name = (body.get("name") or "").strip()
            appearance = body.get("appearance")
            if not name or not isinstance(appearance, dict):
                return jsonify({"error": "name and appearance required"}), 400
            themes = load_themes(themes_path)
            themes[name] = appearance
            save_themes(themes_path, themes)
            return jsonify({"ok": True})
        return jsonify({"themes": load_themes(themes_path)})

    @app.route("/api/search-profiles", methods=["GET"])
    def api_search_profiles_list():
        from .config import load_search_profiles
        from flask import jsonify
        return jsonify(load_search_profiles(config_dir / "search_profiles.json"))

    @app.route("/api/search-profiles", methods=["POST"])
    def api_search_profiles_save():
        from .config import load_search_profiles, save_search_profiles, PROFILE_PREFS
        from flask import jsonify
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        profiles = load_search_profiles(config_dir / "search_profiles.json")
        profiles[name] = {k: config.prefs[k] for k in PROFILE_PREFS if k in config.prefs}
        save_search_profiles(config_dir / "search_profiles.json", profiles)
        return jsonify({"ok": True, "name": name})

    @app.route("/api/search-profiles/<name>", methods=["DELETE"])
    def api_search_profiles_delete(name):
        from .config import load_search_profiles, save_search_profiles
        from flask import jsonify
        profiles = load_search_profiles(config_dir / "search_profiles.json")
        profiles.pop(name, None)
        save_search_profiles(config_dir / "search_profiles.json", profiles)
        return jsonify({"ok": True})

    @app.route("/api/search-profiles/<name>/load", methods=["POST"])
    def api_search_profiles_load(name):
        from .config import load_search_profiles, PROFILE_PREFS, DEFAULT_PREFS, DEFAULT_PROFILES
        from flask import jsonify
        # Built-in profiles always use the latest in-code definition (no restart needed);
        # user-created profiles that share a name with a built-in are superseded.
        profiles = {**load_search_profiles(config_dir / "search_profiles.json"), **DEFAULT_PROFILES}
        if name not in profiles:
            return jsonify({"error": "not found"}), 404
        # Reset all profile-controlled keys to defaults so no bleed-through from the previous profile
        for k in PROFILE_PREFS:
            if k in DEFAULT_PREFS:
                config.prefs[k] = DEFAULT_PREFS[k]
        for k, v in profiles[name].items():
            if k in PROFILE_PREFS:
                config.prefs[k] = v
        config.prefs["active_profile"] = name
        config.prefs["settings_customized"] = False
        save_prefs(config_dir / "preferences.json", config.prefs)
        selector.weighted = config.prefs.get("weighted_selection", True)
        _cw = int(config.prefs.get("circuit_warmup", 0))
        if _cw > 0 and _get_routing(config.prefs) != "direct":
            circuits.start(config.tor_port, pool_size=_cw)
        else:
            circuits.stop()
        # If the profile carries an autopick schedule, fire it immediately in the background
        if config.prefs.get("autopick_schedule", "never") != "never":
            threading.Thread(
                target=_run_autopick, args=(config, config_dir, selector),
                daemon=True, name="ap-profile-load"
            ).start()
            _ap_wake.set()
        return jsonify({"ok": True})

    @app.route("/api/export")
    def api_export():
        import json, datetime
        from flask import Response
        from .config import load_search_profiles, load_themes, _EXPORT_SENSITIVE

        what = request.args.get("what", "all")
        data = {
            "scrambler_export": "1.0",
            "type": what,
            "exported_at": datetime.datetime.utcnow().isoformat() + "Z",
        }

        if what in ("themes", "all"):
            data["themes"] = load_themes(config_dir / "themes.json")

        if what in ("profiles", "all"):
            profiles = load_search_profiles(config_dir / "search_profiles.json")
            # Strip any secrets that may have leaked into older profile saves
            data["profiles"] = {
                pname: {k: v for k, v in pdata.items() if k not in _EXPORT_SENSITIVE}
                for pname, pdata in profiles.items()
            }

        if what in ("settings", "all"):
            data["preferences"] = {k: v for k, v in config.prefs.items() if k not in _EXPORT_SENSITIVE}

        if what == "profile":
            from .config import PROFILE_PREFS
            name = request.args.get("name", "My Profile").strip() or "My Profile"
            data["type"] = "profiles"
            data["profiles"] = {
                name: {k: v for k, v in config.prefs.items()
                       if k in PROFILE_PREFS and k not in _EXPORT_SENSITIVE}
            }

        filename = f"scrambler-{what}-export.json"
        return Response(
            json.dumps(data, indent=2, ensure_ascii=False),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/api/import", methods=["POST"])
    def api_import():
        import json as _json
        from flask import jsonify
        from .config import (load_search_profiles, save_search_profiles,
                             load_themes, save_themes, _EXPORT_SENSITIVE, PROFILE_PREFS)

        body = request.get_json(force=True) or {}
        if body.get("scrambler_export") != "1.0":
            return jsonify({"error": "Not a valid Scrambler export file (missing version marker)"}), 400

        applied = []

        if "themes" in body and isinstance(body["themes"], dict):
            themes = load_themes(config_dir / "themes.json")
            themes.update(body["themes"])
            save_themes(config_dir / "themes.json", themes)
            applied.append(f"{len(body['themes'])} theme(s)")

        if "profiles" in body and isinstance(body["profiles"], dict):
            profiles = load_search_profiles(config_dir / "search_profiles.json")
            for pname, pdata in body["profiles"].items():
                if isinstance(pdata, dict):
                    profiles[pname] = {k: v for k, v in pdata.items() if k not in _EXPORT_SENSITIVE}
            save_search_profiles(config_dir / "search_profiles.json", profiles)
            applied.append(f"{len(body['profiles'])} profile(s)")

        if "preferences" in body and isinstance(body["preferences"], dict):
            for k, v in body["preferences"].items():
                if k not in _EXPORT_SENSITIVE:
                    config.prefs[k] = v
            save_prefs(config_dir / "preferences.json", config.prefs)
            applied.append("preferences")

        if not applied:
            return jsonify({"error": "Nothing recognised to import in this file"}), 400

        return jsonify({"ok": True, "applied": applied})

    @app.route("/api/themes/<name>", methods=["DELETE"])
    def api_theme_delete(name):
        from flask import jsonify
        themes_path = config_dir / "themes.json"
        themes = load_themes(themes_path)
        themes.pop(name, None)
        save_themes(themes_path, themes)
        return jsonify({"ok": True})

    @app.route("/api/ai-personalities", methods=["GET"])
    def api_ai_personalities_list():
        from flask import jsonify
        return jsonify(load_ai_personalities(config_dir / "ai_personalities.json"))

    @app.route("/api/ai-personalities", methods=["POST"])
    def api_ai_personalities_save():
        from flask import jsonify
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        personalities = load_ai_personalities(config_dir / "ai_personalities.json")
        body_data = body.get("data") or {}
        if body_data:
            personalities[name] = {k: v for k, v in body_data.items() if k in AI_PERSONALITY_KEYS}
        else:
            personalities[name] = {k: config.prefs[k] for k in AI_PERSONALITY_KEYS if k in config.prefs}
        save_ai_personalities(config_dir / "ai_personalities.json", personalities)
        return jsonify({"ok": True})

    @app.route("/api/ai-personalities/<name>", methods=["DELETE"])
    def api_ai_personalities_delete(name):
        from flask import jsonify
        personalities = load_ai_personalities(config_dir / "ai_personalities.json")
        personalities.pop(name, None)
        save_ai_personalities(config_dir / "ai_personalities.json", personalities)
        return jsonify({"ok": True})

    @app.route("/api/ai-personalities/<name>/load", methods=["POST"])
    def api_ai_personalities_load(name):
        from flask import jsonify
        personalities = load_ai_personalities(config_dir / "ai_personalities.json")
        if name not in personalities:
            return jsonify({"error": "not found"}), 404
        for k, v in personalities[name].items():
            if k in AI_PERSONALITY_KEYS:
                config.prefs[k] = v
        save_prefs(config_dir / "preferences.json", config.prefs)
        return jsonify({"ok": True})

    @app.route("/api/instance-engines", methods=["POST"])
    def api_instance_engines():
        from flask import jsonify
        body = request.get_json(force=True) or {}
        engine_map = body.get("engine_map") or {}
        if not isinstance(engine_map, dict):
            return jsonify({"error": "engine_map must be a dict"}), 400
        existing = load_instance_engines(config_dir / "instance_engines.json")
        existing.update({k.rstrip("/"): v for k, v in engine_map.items() if isinstance(v, list)})
        save_instance_engines(config_dir / "instance_engines.json", existing)
        selector.set_engine_map(existing)
        return jsonify({"ok": True})

    @app.route("/api/discover")
    def api_discover():
        from .discover import fetch_instances
        from flask import jsonify
        min_grade = request.args.get("min_grade", "").strip() or None
        instances, error = fetch_instances(min_grade=min_grade)
        return jsonify({"instances": instances, "error": error})

    @app.route("/api/launcher", methods=["GET"])
    def api_launcher_get():
        from . import launcher
        from flask import jsonify
        return jsonify({"installed": launcher.is_installed()})

    @app.route("/api/launcher", methods=["POST"])
    def api_launcher_post():
        from . import launcher
        from flask import jsonify
        data = request.get_json(force=True)
        install = bool(data.get("install"))
        error = launcher.create(config_dir, config.prefs.get("appearance")) if install else launcher.remove()
        if error:
            return jsonify({"error": error}), 500
        return jsonify({"installed": install})

    @app.route("/api/autostart", methods=["GET"])
    def api_autostart_get():
        from . import autostart
        from flask import jsonify
        return jsonify({"enabled": autostart.is_enabled(), "available": autostart.available()})

    @app.route("/api/autostart", methods=["POST"])
    def api_autostart_post():
        from . import autostart
        from flask import jsonify
        data = request.get_json(force=True)
        enable = bool(data.get("enable"))
        error = autostart.enable() if enable else autostart.disable()
        if error:
            return jsonify({"error": error}), 500
        return jsonify({"enabled": enable})

    @app.route("/api/sem-install", methods=["POST"])
    def api_sem_install():
        from .ranker import start_install, install_status
        from flask import jsonify
        started = start_install()
        return jsonify({"started": started, **install_status()})

    @app.route("/api/sem-install/status")
    def api_sem_install_status():
        from .ranker import install_status
        from flask import jsonify
        return jsonify(install_status())

    @app.route("/api/restart", methods=["POST"])
    def api_restart():
        import os, sys, shlex, subprocess, threading
        from flask import jsonify
        exe  = sys.executable
        cmd  = " ".join(shlex.quote(a) for a in [exe] + sys.argv)
        # Shell helper: wait for this process to die, then re-launch
        shell = f"sleep 0.6 && exec {cmd} >> /tmp/scrambler.log 2>&1"
        def _do():
            import time
            time.sleep(0.2)  # let HTTP response flush
            subprocess.Popen(["bash", "-c", shell], start_new_session=True)
            os._exit(0)
        threading.Thread(target=_do, daemon=True).start()
        return jsonify({"ok": True})

    @app.route("/api/dearrow")
    def api_dearrow():
        from flask import jsonify
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import requests as _req

        ids_raw = request.args.get("ids", "").strip()
        if not ids_raw:
            return jsonify({}), 400
        vid_ids = [i.strip() for i in ids_raw.split(",") if i.strip()][:20]

        routing = _get_routing(config.prefs)

        def _fetch_branding(vid_id: str):
            url = f"https://sponsor.ajay.app/api/branding?videoID={vid_id}"
            proxies = {}
            if routing in ("tor", "tor_fallback"):
                try:
                    cred = circuits.get_credential()
                    p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                    proxies = {"http": p, "https": p}
                except Exception:
                    if routing != "tor_fallback":
                        return vid_id, None
            try:
                resp = _req.get(url, proxies=proxies, timeout=5,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"})
                if resp.status_code == 200:
                    return vid_id, resp.json()
                if routing == "tor_fallback" and proxies:
                    resp = _req.get(url, proxies={}, timeout=5,
                                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"})
                    if resp.status_code == 200:
                        return vid_id, resp.json()
            except Exception:
                pass
            return vid_id, None

        results = {}
        with ThreadPoolExecutor(max_workers=min(len(vid_ids), 8)) as ex:
            futures = {ex.submit(_fetch_branding, vid): vid for vid in vid_ids}
            for fut in as_completed(futures, timeout=8):
                try:
                    vid_id, data = fut.result()
                    if data:
                        results[vid_id] = data
                except Exception:
                    pass
        return jsonify(results)

    @app.route("/api/dearrow/thumb")
    def api_dearrow_thumb():
        import requests as _req
        from flask import Response
        vid = request.args.get("videoID", "").strip()
        time_s = request.args.get("time", "").strip()
        if not vid or not time_s:
            return "", 400
        url = f"https://dearrow-thumb.ajay.app/api/v1/getThumbnail?videoID={vid}&time={time_s}&generateNow=1"
        routing = _get_routing(config.prefs)
        proxies = {}
        if routing in ("tor", "tor_fallback"):
            try:
                cred = circuits.get_credential()
                p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                proxies = {"http": p, "https": p}
            except Exception:
                if routing != "tor_fallback":
                    return "", 503
        _da_ft = int(config.prefs.get('failover_timeout', 12))
        _da_tor_t = _da_ft if routing == "tor_fallback" else 10
        try:
            resp = _req.get(url, proxies=proxies, timeout=_da_tor_t, stream=True,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"})
            if resp.status_code != 200 and routing == "tor_fallback" and proxies:
                resp = _req.get(url, proxies={}, timeout=10, stream=True,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"})
            if resp.status_code != 200:
                return "", resp.status_code
            ct = resp.headers.get("Content-Type", "image/jpeg")
            return Response(resp.content, content_type=ct,
                            headers={"Cache-Control": "public, max-age=86400"})
        except Exception:
            return "", 503

    @app.route("/api/reset-cooldowns", methods=["POST"])
    def api_reset_cooldowns():
        from flask import jsonify
        selector.update_instances(config.instances)  # re-inserts all with fresh _Health()
        # also force all existing health entries healthy
        for url in list(selector._health):
            h = selector._health[url]
            h.healthy = True
            h.failures = 0
            h.cooldown_until = 0.0
        return jsonify({"ok": True, "count": len(config.instances)})

    @app.route("/api/tile/<int:z>/<int:x>/<int:y>")
    def api_tile_proxy(z, x, y):
        import requests as _req
        import random as _rnd

        # Validate tile coordinates
        if not (0 <= z <= 20 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            return "", 400

        provider = config.prefs.get("map_tile_provider", "openstreetmap")
        custom_url = config.prefs.get("map_tile_custom_url", "").strip()

        _TILE_TEMPLATES = {
            "openstreetmap": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            "carto_light":   "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
            "carto_dark":    "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        }

        if provider == "custom" and custom_url:
            template = custom_url
        elif provider in _TILE_TEMPLATES:
            template = _TILE_TEMPLATES[provider]
        else:
            return "", 404

        s = _rnd.choice(["a", "b", "c"])
        url = (template
               .replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
               .replace("{s}", s).replace("{r}", ""))

        routing = _get_tile_routing(config.prefs)
        proxies = {}
        if routing in ("tor", "tor_fallback"):
            try:
                cred = circuits.get_credential()
                p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                proxies = {"http": p, "https": p}
            except Exception:
                if routing != "tor_fallback":
                    return "", 503

        _tile_ft = int(config.prefs.get("failover_timeout", 12))
        _tile_tor_t = _tile_ft if routing == "tor_fallback" else 15
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        }
        try:
            resp = _req.get(url, proxies=proxies, timeout=_tile_tor_t, headers=headers)
            if resp.status_code != 200 and routing == "tor_fallback" and proxies:
                resp = _req.get(url, proxies={}, timeout=10, headers=headers)
            if resp.status_code != 200:
                return "", resp.status_code
            ct = resp.headers.get("Content-Type", "image/png")
            return Response(resp.content, content_type=ct,
                            headers={"Cache-Control": "public, max-age=86400"})
        except Exception:
            return "", 503

    @app.route("/api/nominatim")
    def api_nominatim():
        from flask import jsonify
        import requests as _req

        q = request.args.get("q", "").strip()
        if not q:
            return jsonify([]), 400

        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": q, "format": "json", "limit": 20, "addressdetails": 1}
        countrycodes = request.args.get("countrycodes", "").strip()
        if countrycodes:
            params["countrycodes"] = countrycodes
        viewbox = request.args.get("viewbox", "").strip()
        if viewbox:
            params["viewbox"] = viewbox
            params["bounded"] = request.args.get("bounded", "1")
        headers = {
            "User-Agent": "Scrambler/1.0",
            "Accept-Language": config.prefs.get("language", "en-US"),
        }

        routing = _get_map_routing(config.prefs)
        proxies = {}
        if routing in ("tor", "tor_fallback"):
            try:
                cred = circuits.get_credential()
                p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                proxies = {"http": p, "https": p}
            except Exception:
                if routing != "tor_fallback":
                    return jsonify({"error": "Tor unavailable"}), 503

        _nom_ft = int(config.prefs.get('failover_timeout', 12))
        _nom_tor_t = _nom_ft if routing == "tor_fallback" else 10
        try:
            resp = _req.get(url, params=params, proxies=proxies,
                            timeout=_nom_tor_t, headers=headers)
            if resp.status_code != 200 and routing == "tor_fallback" and proxies:
                resp = _req.get(url, params=params, proxies={},
                                timeout=10, headers=headers)
            if resp.status_code != 200:
                return jsonify({"error": f"Nominatim returned {resp.status_code}"}), 502
            return jsonify(resp.json())
        except Exception as e:
            return jsonify({"error": str(e)}), 503

    def _decode_polyline(enc):
        coords, i, lat, lng = [], 0, 0, 0
        while i < len(enc):
            result = shift = 0
            while True:
                b = ord(enc[i]) - 63; i += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20: break
            lat += (~(result >> 1) if result & 1 else result >> 1)
            result = shift = 0
            while True:
                b = ord(enc[i]) - 63; i += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20: break
            lng += (~(result >> 1) if result & 1 else result >> 1)
            coords.append([lat / 1e5, lng / 1e5])
        return coords

    def _norm_otp_leg(leg):
        pts = (leg.get("legGeometry") or {}).get("points", "")
        coords = _decode_polyline(pts) if pts else []
        return {
            "mode": leg.get("mode", "WALK"),
            "coords": coords,
            "line": leg.get("routeShortName") or "",
            "headsign": leg.get("headsign") or "",
            "from_name": (leg.get("from") or {}).get("name", ""),
            "to_name": (leg.get("to") or {}).get("name", ""),
            "start_time": leg.get("startTime", 0),
            "end_time": leg.get("endTime", 0),
            "distance": leg.get("distance", 0),
            "color": None,
        }

    def _norm_navitia_section(s):
        from datetime import datetime as _dt
        _MODE = {"walking": "WALK", "Bus": "BUS", "Metro": "SUBWAY", "Subway": "SUBWAY",
                 "RapidTransit": "RAIL", "Rail": "RAIL", "Tramway": "TRAM", "Ferry": "FERRY",
                 "Funicular": "TRAM", "Cable car": "TRAM"}
        stype = s.get("type", "")
        if stype not in ("public_transport", "street_network"):
            return None
        di = s.get("display_informations", {})
        mode = "WALK" if stype == "street_network" else _MODE.get(di.get("physical_mode", "Bus"), "BUS")
        coords = [[c[1], c[0]] for c in (s.get("geojson") or {}).get("coordinates", [])]

        def _parse_dt(s):
            try:
                return int(_dt.strptime(s, "%Y%m%dT%H%M%S").timestamp() * 1000)
            except Exception:
                return 0

        color_hex = di.get("color", "")
        return {
            "mode": mode,
            "coords": coords,
            "line": di.get("code") or di.get("label") or "",
            "headsign": di.get("direction") or di.get("headsign") or "",
            "from_name": (s.get("from") or {}).get("name", ""),
            "to_name": (s.get("to") or {}).get("name", ""),
            "start_time": _parse_dt(s.get("departure_date_time", "")),
            "end_time": _parse_dt(s.get("arrival_date_time", "")),
            "distance": s.get("geojson_length") or 0,
            "color": ("#" + color_hex) if color_hex else None,
        }

    @app.route("/api/route")
    def api_route():
        from flask import jsonify
        import requests as _req
        from datetime import datetime

        waypoints_str = request.args.get("waypoints", "")
        if waypoints_str:
            wps = []
            for wp_str in waypoints_str.split(";"):
                parts = wp_str.split(",")
                try:
                    wps.append((float(parts[0]), float(parts[1])))
                except (IndexError, ValueError):
                    pass
            if len(wps) < 2:
                return jsonify({"error": "waypoints must have at least 2 lat,lon pairs separated by ;"}), 400
        else:
            try:
                from_lat = float(request.args["from_lat"])
                from_lon = float(request.args["from_lon"])
                to_lat   = float(request.args["to_lat"])
                to_lon   = float(request.args["to_lon"])
                wps = [(from_lat, from_lon), (to_lat, to_lon)]
            except (KeyError, ValueError, TypeError):
                return jsonify({"error": "waypoints or from_lat/from_lon/to_lat/to_lon required"}), 400

        mode = request.args.get("mode", "driving")
        headers = {"User-Agent": "Scrambler/1.0"}

        routing = _get_map_routing(config.prefs)
        proxies = {}
        if routing in ("tor", "tor_fallback"):
            try:
                cred = circuits.get_credential()
                p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                proxies = {"http": p, "https": p}
            except Exception:
                if routing != "tor_fallback":
                    return jsonify({"error": "Tor unavailable"}), 503

        _route_ft = int(config.prefs.get('failover_timeout', 12))
        _route_tor_t = _route_ft if routing == "tor_fallback" else 20

        def _get(url, params):
            resp = _req.get(url, params=params, proxies=proxies, timeout=_route_tor_t, headers=headers)
            if resp.status_code != 200 and routing == "tor_fallback" and proxies:
                resp = _req.get(url, params=params, proxies={}, timeout=20, headers=headers)
            return resp

        # ── Transit routing (point-to-point only: first and last waypoint) ──
        from_lat, from_lon = wps[0]
        to_lat, to_lon = wps[-1]
        if mode == "transit":
            navitia_key = config.prefs.get("transit_navitia_key", "").strip()
            now = datetime.now()

            if navitia_key:
                # ── Navitia.io (good US coverage) ─────────────────────────
                url = "https://api.navitia.io/v1/journeys"
                params = {
                    "from": f"{from_lon};{from_lat}",   # Navitia uses lon;lat
                    "to":   f"{to_lon};{to_lat}",
                    "datetime": now.strftime("%Y%m%dT%H%M%S"),
                    "count": 3,
                }
                nav_headers = {**headers, "Authorization": navitia_key}
                try:
                    resp = _req.get(url, params=params, headers=nav_headers,
                                    proxies=proxies, timeout=20)
                    if resp.status_code == 401:
                        return jsonify({"error": "Invalid Navitia API key — check Settings → Transit."}), 401
                    if resp.status_code != 200:
                        return jsonify({"error": f"Navitia returned {resp.status_code}"}), 502
                    data = resp.json()
                    journeys = data.get("journeys", [])
                    if not journeys:
                        return jsonify({"error": "No transit routes found for this trip."}), 404
                    itineraries = []
                    for j in journeys:
                        legs = [l for l in (_norm_navitia_section(s) for s in j.get("sections", [])) if l]
                        itineraries.append({"duration": j.get("duration", 0), "legs": legs})
                    return jsonify({"type": "transit", "itineraries": itineraries})
                except Exception as e:
                    return jsonify({"error": str(e)}), 503

            else:
                # ── OTP / Transitous (better European coverage) ───────────
                transit_base = config.prefs.get("route_transit_url", "https://api.transitous.org/otp").rstrip("/")
                url = f"{transit_base}/routers/default/plan"
                params = {
                    "fromPlace": f"{from_lat},{from_lon}",
                    "toPlace":   f"{to_lat},{to_lon}",
                    "mode":      "TRANSIT,WALK",
                    "date":      now.strftime("%Y-%m-%d"),
                    "time":      now.strftime("%H:%M:%S"),
                    "numItineraries": 3,
                }
                try:
                    resp = _get(url, params)
                    if resp.status_code != 200:
                        return jsonify({"error": (
                            f"Transit API returned {resp.status_code}. "
                            "Transitous has limited US coverage — add a Navitia API key in Settings for US cities."
                        )}), 502
                    data = resp.json()
                    if data.get("error"):
                        code = data["error"].get("id", "")
                        msg  = data["error"].get("msg", "No transit route found")
                        if code == 404:
                            msg = ("No transit routes found. Transitous covers cities with open GTFS data — "
                                   "for US cities add a free Navitia API key in Settings.")
                        return jsonify({"error": msg}), 404
                    plan = data.get("plan", {})
                    if not plan.get("itineraries"):
                        return jsonify({"error": (
                            "No transit itineraries found. "
                            "For US cities, add a free Navitia API key in Settings → Transit."
                        )}), 404
                    itineraries = []
                    for itin in plan["itineraries"]:
                        legs = [_norm_otp_leg(l) for l in itin.get("legs", [])]
                        itineraries.append({"duration": itin.get("duration", 0), "legs": legs})
                    return jsonify({"type": "transit", "itineraries": itineraries})
                except Exception as e:
                    return jsonify({"error": str(e)}), 503

        # ── Road routing (OSRM) ─────────────────────────────────────────
        osrm_profile = {"driving": "car", "walking": "foot", "cycling": "bike"}.get(mode, "car")
        route_provider = config.prefs.get("route_provider", "osrm_public")
        route_custom_url = config.prefs.get("route_custom_url", "").rstrip("/")
        base = route_custom_url if (route_provider == "custom" and route_custom_url) else "https://router.project-osrm.org"
        coord_str = ";".join(f"{wp[1]},{wp[0]}" for wp in wps)
        url = f"{base}/route/v1/{osrm_profile}/{coord_str}"
        params = {"overview": "full", "geometries": "geojson", "steps": "true", "annotations": "false"}

        try:
            resp = _get(url, params)
            if resp.status_code != 200:
                return jsonify({"error": f"OSRM returned {resp.status_code}"}), 502
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                return jsonify({"error": "No route found"}), 404
            route = data["routes"][0]
            all_steps = []
            for leg in route.get("legs", []):
                all_steps.extend(leg.get("steps", []))
            return jsonify({
                "type": "road",
                "geometry": route["geometry"],
                "distance": route["distance"],
                "duration": route["duration"],
                "steps": all_steps,
                "waypoints": [{"lat": wp[0], "lon": wp[1]} for wp in wps],
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 503

    # ── Map saved locations ──────────────────────────────────────────────
    @app.route("/api/map-saved-locations", methods=["GET", "POST", "DELETE"])
    def api_map_saved_locations():
        from flask import jsonify
        path = config_dir / "map_saved_locations.json"
        data = load_map_data(path)
        if request.method == "GET":
            return jsonify(data)
        body = request.get_json(force=True) or {}
        if request.method == "POST":
            try:
                item = {
                    "name": str(body.get("name", ""))[:100].strip(),
                    "lat": float(body["lat"]),
                    "lon": float(body["lon"]),
                }
            except (KeyError, ValueError, TypeError):
                return jsonify({"error": "name, lat, lon required"}), 400
            if body.get("address"):
                item["address"] = str(body["address"])[:300]
            if body.get("icon"):
                item["icon"] = str(body["icon"])[:10]
            if body.get("color"):
                item["color"] = str(body["color"])[:20]
            old_name = str(body.get("old_name", "")).strip()
            if old_name and old_name != item["name"]:
                data = [d for d in data if d.get("name") != old_name]
            for i, d in enumerate(data):
                if d.get("name") == item["name"]:
                    data[i] = item
                    break
            else:
                data.append(item)
            save_map_data(path, data)
            return jsonify({"ok": True})
        if request.method == "DELETE":
            name = body.get("name")
            data = [d for d in data if d.get("name") != name]
            save_map_data(path, data)
            return jsonify({"ok": True})
        return jsonify({"error": "method not allowed"}), 405

    # ── Map saved routes ─────────────────────────────────────────────────
    @app.route("/api/map-saved-routes", methods=["GET", "POST", "DELETE"])
    def api_map_saved_routes():
        from flask import jsonify
        path = config_dir / "map_saved_routes.json"
        data = load_map_data(path)
        if request.method == "GET":
            return jsonify(data)
        body = request.get_json(force=True) or {}
        if request.method == "POST":
            item = {
                "name": str(body.get("name", ""))[:200].strip(),
                "waypoints": body.get("waypoints") or [],
                "mode": str(body.get("mode", "driving")),
                "distance": body.get("distance"),
                "duration": body.get("duration"),
                "layer_id": str(body.get("layer_id", ""))[:100],
            }
            if not item["name"] or not item["waypoints"]:
                return jsonify({"error": "name and waypoints required"}), 400
            data.append(item)
            save_map_data(path, data)
            return jsonify({"ok": True, "index": len(data) - 1})
        if request.method == "DELETE":
            idx = body.get("index")
            if isinstance(idx, int) and 0 <= idx < len(data):
                data.pop(idx)
                save_map_data(path, data)
            return jsonify({"ok": True})
        return jsonify({"error": "method not allowed"}), 405

    # ── Map objects (user-placed markers, lines, polygons) ───────────────
    @app.route("/api/map-objects", methods=["GET", "POST", "DELETE"])
    def api_map_objects():
        from flask import jsonify
        import uuid as _uuid
        path = config_dir / "map_objects.json"
        data = load_map_data(path)
        if request.method == "GET":
            return jsonify(data)
        body = request.get_json(force=True) or {}
        if request.method == "POST":
            obj_type = str(body.get("type", "marker"))
            if obj_type not in ("marker", "line", "polygon"):
                return jsonify({"error": "type must be marker, line, or polygon"}), 400
            existing_id = str(body.get("id", "")).strip()
            item = {
                "id": existing_id or str(_uuid.uuid4()),
                "type": obj_type,
                "name": str(body.get("name", ""))[:100].strip() or "Object",
                "coords": body.get("coords") or [],
                "icon":  str(body.get("icon",  "📍"))[:10],
                "color": str(body.get("color", ""))[:20],
                "layer_id": str(body.get("layer_id", ""))[:100],
                "visible": bool(body.get("visible", True)),
            }
            if existing_id:
                for i, d in enumerate(data):
                    if d.get("id") == existing_id:
                        item["coords"] = item["coords"] or d.get("coords", [])
                        data[i] = item
                        break
                else:
                    if not item["coords"]:
                        return jsonify({"error": "coords required"}), 400
                    data.append(item)
            else:
                if not item["coords"]:
                    return jsonify({"error": "coords required"}), 400
                data.append(item)
            save_map_data(path, data)
            return jsonify({"ok": True, "id": item["id"]})
        if request.method == "DELETE":
            obj_id = body.get("id")
            data = [d for d in data if d.get("id") != obj_id]
            save_map_data(path, data)
            return jsonify({"ok": True})
        return jsonify({"error": "method not allowed"}), 405

    @app.route("/api/map-layers", methods=["GET", "POST", "DELETE"])
    def api_map_layers():
        from flask import jsonify
        import uuid as _uuid
        path = config_dir / "map_layers.json"
        data = load_map_data(path)
        if request.method == "GET":
            if not data:
                default = {"id": str(_uuid.uuid4()), "name": "Default Layer", "color": "#5b9cf6", "visible": True}
                data.append(default)
                save_map_data(path, data)
            return jsonify(data)
        body = request.get_json(force=True) or {}
        if request.method == "POST":
            existing_id = str(body.get("id", "")).strip()
            item = {
                "id": existing_id or str(_uuid.uuid4()),
                "name": str(body.get("name", "Layer"))[:100].strip() or "Layer",
                "color": str(body.get("color", "#5b9cf6"))[:20],
                "visible": bool(body.get("visible", True)),
            }
            if existing_id:
                for i, d in enumerate(data):
                    if d.get("id") == existing_id:
                        data[i] = item
                        break
                else:
                    data.append(item)
            else:
                data.append(item)
            save_map_data(path, data)
            return jsonify({"ok": True, "id": item["id"]})
        if request.method == "DELETE":
            layer_id = body.get("id")
            data = [d for d in data if d.get("id") != layer_id]
            save_map_data(path, data)
            return jsonify({"ok": True})
        return jsonify({"error": "method not allowed"}), 405

    @app.route("/api/ai-summary", methods=["POST"])
    def api_ai_summary():
        import html as _html
        import re as _re
        import requests as _req
        from flask import jsonify
        from urllib.parse import urlparse as _up

        body = request.get_json(force=True) or {}
        query = (body.get("query") or "").strip()[:500]
        raw_results = body.get("results") or []
        incoming_messages = body.get("messages") or []
        if not isinstance(raw_results, list):
            return jsonify({"error": "results must be a list"}), 400
        if not isinstance(incoming_messages, list):
            incoming_messages = []

        # All AI config comes from server-side prefs only — never from the request body
        ai_provider  = config.prefs.get("ai_provider", "").strip()
        ai_api_key   = config.prefs.get("ai_api_key", "").strip()
        ai_base_url  = config.prefs.get("ai_base_url", "").strip().rstrip("/")
        ai_model     = config.prefs.get("ai_model", "").strip()
        ai_routing   = config.prefs.get("ai_routing", "direct")
        ai_max       = max(1, min(20, int(config.prefs.get("ai_max_results", 10) or 10)))
        ai_sys       = config.prefs.get("ai_system_prompt", "").strip()

        if not ai_provider:
            return jsonify({"error": "No AI provider configured. Set one in Settings → AI."}), 400
        if not ai_model:
            return jsonify({"error": "No AI model configured. Set one in Settings → AI."}), 400
        if ai_provider in ("anthropic", "openai_compat") and not ai_api_key:
            return jsonify({"error": "No API key configured for this provider."}), 400

        def _strip_tags(s):
            return _re.sub(r'<[^>]+>', '', str(s or ''))

        def _strip_md(s):
            s = _re.sub(r'^#{1,6}\s+', '', s, flags=_re.MULTILINE)
            s = _re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', s)
            s = _re.sub(r'!\[([^\]]*)\]\([^\)]+\)', r'\1', s)
            s = _re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', s)
            s = _re.sub(r'```[^\n]*\n?', '', s)
            s = _re.sub(r'`([^`]+)`', r'\1', s)
            s = _re.sub(r'^[-*_]{3,}\s*$', '', s, flags=_re.MULTILINE)
            s = _re.sub(r'\n{3,}', '\n\n', s)
            return s.strip()

        ai_use_jina  = config.prefs.get("ai_use_jina", False)
        jina_routing = config.prefs.get("jina_routing", "direct")

        jina_contents: dict = {}
        if ai_use_jina and not incoming_messages:
            from concurrent.futures import ThreadPoolExecutor, as_completed as _afc
            j_proxies: dict = {}
            if jina_routing in ("tor", "tor_fallback"):
                try:
                    _jcred = circuits.get_credential()
                    _jp = f"socks5h://{_jcred}:x@127.0.0.1:{config.tor_port}"
                    j_proxies = {"http": _jp, "https": _jp}
                except Exception:
                    if jina_routing == "tor":
                        return jsonify({"error": "Tor unavailable for Jina content fetch."}), 503

            _jina_key = config.prefs.get("jina_api_key", "").strip()
            _jina_hdrs = {
                "X-Return-Format": "markdown",
                "User-Agent": "Mozilla/5.0 (compatible; Scrambler/1.0)",
            }
            if _jina_key:
                _jina_hdrs["Authorization"] = f"Bearer {_jina_key}"

            _j_using_tor = bool(j_proxies)

            def _fetch_jina(r):
                _url = str(r.get("url") or "")[:500]
                if not _url:
                    return _url, None
                try:
                    resp = _req.get(
                        "https://r.jina.ai/" + _url,
                        proxies=j_proxies,
                        headers=_jina_hdrs,
                        timeout=15,
                        allow_redirects=True,
                    )
                    # tor_fallback: if Tor attempt blocked, retry direct
                    if not resp.ok and _j_using_tor and jina_routing == "tor_fallback":
                        resp = _req.get(
                            "https://r.jina.ai/" + _url,
                            proxies={},
                            headers=_jina_hdrs,
                            timeout=15,
                            allow_redirects=True,
                        )
                    if resp.ok:
                        text = resp.text
                        # Detect Jina error pages (200 OK but content is an error/redirect page)
                        first_lines = text[:500].lower()
                        if "warning:" in first_lines or "can't find" in first_lines or "not found" in first_lines or "error" in first_lines[:100]:
                            return _url, None
                        # Strip Jina metadata header
                        mc = text.find("Markdown Content:")
                        if mc != -1:
                            text = text[mc + len("Markdown Content:"):].strip()
                        # Remove lines that are purely nav links — [text](url) with nothing else
                        _link_only = _re.compile(r'^\s*(\[[^\]]*\]\([^\)]*\)\s*[|·•]?\s*)+\s*$')
                        lines = text.split('\n')
                        cleaned = [l for l in lines if not _link_only.match(l)]
                        # Collapse runs of 3+ blank lines left by removed nav blocks
                        text = _re.sub(r'\n{3,}', '\n\n', '\n'.join(cleaned)).strip()
                        return _url, text[:12000]
                except Exception:
                    pass
                return _url, None

            with ThreadPoolExecutor(max_workers=5) as _ex:
                _futs = {_ex.submit(_fetch_jina, r): r for r in raw_results[:ai_max]}
                for _f in _afc(_futs):
                    _u, _c = _f.result()
                    if _c:
                        jina_contents[_u] = _c

        results_text = ""
        for i, r in enumerate(raw_results[:ai_max]):
            title   = _strip_tags(r.get("title") or "")[:200]
            url     = str(r.get("url") or "")[:500]
            snippet = _strip_tags(r.get("snippet") or "")[:500]
            if ai_use_jina and url in jina_contents:
                results_text += f"\n[{i+1}] {title}\n{url}\n{_strip_md(jina_contents[url])}\n"
            else:
                results_text += f"\n[{i+1}] {title}\n{url}\n{snippet}\n"

        if not results_text.strip():
            return jsonify({"error": "No results to summarize."}), 400

        if ai_use_jina and jina_contents:
            default_sys = (
                "You are a research assistant. You have been given the full extracted text from each result page — "
                "not just snippets. Use this rich content to give detailed, accurate answers. "
                "Cite specific facts, figures, and quotes from the pages where relevant. "
                "Be objective. Do not invent information not present in the provided content."
            )
        else:
            default_sys = (
                "You are a research assistant. Given a search query and search results, "
                "write a concise summary (3-5 sentences) of the key findings. "
                "Be objective and specific. Do not invent information not present in the results."
            )
        system_prompt = ai_sys or default_sys
        user_msg = f'Search query: "{query}"\n\nSearch results:\n{results_text}'

        # Build message list — use incoming history for follow-ups, or start fresh
        sanitized = [
            {"role": m["role"], "content": str(m["content"])[:8000]}
            for m in incoming_messages
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
        ]
        messages_to_send = sanitized if sanitized else [{"role": "user", "content": user_msg}]

        # Local addresses always bypass Tor regardless of ai_routing setting
        target_host = (_up(ai_base_url or "https://api.anthropic.com").hostname or "").lower()
        effective_routing = "direct" if _PRIVATE_HOST_RE.match(target_host) else ai_routing

        proxies = {}
        if effective_routing in ("tor", "tor_fallback"):
            try:
                cred = circuits.get_credential()
                p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                proxies = {"http": p, "https": p}
            except Exception:
                if effective_routing != "tor_fallback":
                    return jsonify({"error": "Tor unavailable for AI request."}), 503

        hdrs = {"Content-Type": "application/json", "User-Agent": "Scrambler/1.0"}

        def _call(use_proxies):
            if ai_provider == "anthropic":
                url = "https://api.anthropic.com/v1/messages"
                h = {**hdrs, "x-api-key": ai_api_key, "anthropic-version": "2023-06-01"}
                payload = {
                    "model": ai_model, "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": messages_to_send,
                }
                resp = _req.post(url, json=payload, headers=h, proxies=use_proxies, timeout=30)
                if resp.status_code == 401:
                    raise Exception("Invalid API key (401)")
                if resp.status_code == 403:
                    raise Exception("Access forbidden (403) — provider may block Tor exit IPs")
                if resp.status_code == 429:
                    raise Exception("Rate limited (429) — try again later")
                if not resp.ok:
                    try:
                        detail = resp.json()
                        msg = (detail.get("error") or {}).get("message") or str(detail)
                    except Exception:
                        msg = resp.text[:200]
                    raise Exception(f"Anthropic API {resp.status_code}: {msg}")
                return resp.json()["content"][0]["text"]
            else:
                base = (ai_base_url or "https://api.openai.com").rstrip("/")
                # Strip trailing /v1 so users can paste either the root or the /v1 base URL
                if base.endswith("/v1"):
                    base = base[:-3]
                url = base + "/v1/chat/completions"
                h = {**hdrs}
                if ai_api_key:
                    h["Authorization"] = f"Bearer {ai_api_key}"
                payload = {
                    "model": ai_model, "max_tokens": 1024,
                    "messages": [{"role": "system", "content": system_prompt}] + messages_to_send,
                }
                resp = _req.post(url, json=payload, headers=h, proxies=use_proxies, timeout=30)
                if resp.status_code == 401:
                    raise Exception("Invalid API key (401)")
                if resp.status_code == 403:
                    raise Exception("Access forbidden (403) — provider may block Tor exit IPs")
                if resp.status_code == 429:
                    raise Exception("Rate limited (429) — try again later")
                if not resp.ok:
                    try:
                        detail = resp.json()
                        msg = (detail.get("error") or {}).get("message") or str(detail)
                    except Exception:
                        msg = resp.text[:200]
                    raise Exception(f"API {resp.status_code}: {msg}")
                return resp.json()["choices"][0]["message"]["content"]

        try:
            summary = _call(proxies)
        except Exception as e:
            if effective_routing == "tor_fallback" and proxies:
                try:
                    summary = _call({})
                except Exception as e2:
                    return jsonify({"error": f"AI request failed: {e2}"}), 503
            else:
                return jsonify({"error": f"AI request failed: {e}"}), 503

        updated_messages = messages_to_send + [{"role": "assistant", "content": summary}]
        return jsonify({"summary": summary, "messages": updated_messages, "jina_fetched": len(jina_contents)})

    @app.route("/api/image-gen/prompt", methods=["POST"])
    def api_image_gen_prompt():
        """LLM step: turn a search query + image result titles into an image-gen prompt."""
        import requests as _req
        from flask import jsonify
        from urllib.parse import urlparse as _up

        body      = request.get_json(force=True, silent=True) or {}
        query     = (body.get("q") or "").strip()[:300]
        context   = body.get("context") or []  # [{title, url}, ...]
        if not query:
            return jsonify({"error": "query required"}), 400

        ai_provider = config.prefs.get("ai_provider", "").strip()
        ai_api_key  = config.prefs.get("ai_api_key", "").strip()
        ai_base_url = config.prefs.get("ai_base_url", "").strip().rstrip("/")
        ai_model    = config.prefs.get("ai_model", "").strip()
        ai_routing  = config.prefs.get("ai_routing", "direct")

        if not ai_provider:
            return jsonify({"error": "No AI provider configured in Settings → AI."}), 400
        if not ai_model:
            return jsonify({"error": "No AI model configured in Settings → AI."}), 400

        ctx_lines = "\n".join(
            f"- {c.get('title','').strip()}"
            for c in context[:20] if c.get("title","").strip()
        )
        user_msg = (
            f'Search query: "{query}"\n\n'
            + (f"Context from image results:\n{ctx_lines}\n\n" if ctx_lines else "")
            + "Write an image generation prompt."
        )
        system_prompt = (
            "You are an expert image generation prompt engineer. "
            "Given a search query and optional context from image search results, "
            "write a detailed prompt for an AI image generator. "
            "Focus on visual details: subject, style, lighting, composition, mood, medium. "
            "Return a JSON object with exactly two string keys: "
            '"prompt" (the positive prompt) and '
            '"negative_prompt" (things to exclude; use standard Stable Diffusion negative terms '
            "or leave empty for DALL-E). Return only the JSON, no explanation."
        )

        target_host      = (_up(ai_base_url or "https://api.anthropic.com").hostname or "").lower()
        effective_routing = "direct" if _PRIVATE_HOST_RE.match(target_host) else ai_routing
        proxies = {}
        if effective_routing in ("tor", "tor_fallback"):
            try:
                cred = circuits.get_credential()
                p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                proxies = {"http": p, "https": p}
            except Exception:
                if effective_routing != "tor_fallback":
                    return jsonify({"error": "Tor unavailable."}), 503

        hdrs = {"Content-Type": "application/json", "User-Agent": "Scrambler/1.0"}

        def _call(px):
            import json as _json
            if ai_provider == "anthropic":
                resp = _req.post(
                    "https://api.anthropic.com/v1/messages",
                    json={"model": ai_model, "max_tokens": 1024,
                          "system": system_prompt,
                          "messages": [{"role": "user", "content": user_msg}]},
                    headers={**hdrs, "x-api-key": ai_api_key, "anthropic-version": "2023-06-01"},
                    proxies=px, timeout=30,
                )
                resp.raise_for_status()
                raw = resp.json()["content"][0]["text"]
            else:
                base = (ai_base_url or "https://api.openai.com").rstrip("/")
                if base.endswith("/v1"):
                    base = base[:-3]
                h = {**hdrs}
                if ai_api_key:
                    h["Authorization"] = f"Bearer {ai_api_key}"
                resp = _req.post(
                    base + "/v1/chat/completions",
                    json={"model": ai_model, "max_tokens": 1024,
                          "messages": [{"role": "system", "content": system_prompt},
                                       {"role": "user",   "content": user_msg}]},
                    headers=h, proxies=px, timeout=30,
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"]
            # Extract JSON from response (LLM may wrap it in markdown)
            import re as _re
            m = _re.search(r'\{[\s\S]*?\}', raw)
            if not m:
                return {"prompt": raw.strip(), "negative_prompt": ""}
            try:
                return _json.loads(m.group(0))
            except Exception:
                # Greedy fallback: match outermost braces
                m2 = _re.search(r'\{[\s\S]*\}', raw)
                if m2:
                    try:
                        return _json.loads(m2.group(0))
                    except Exception:
                        pass
                # Last resort: return the raw text as the prompt
                return {"prompt": raw.strip(), "negative_prompt": ""}

        try:
            result = _call(proxies)
        except Exception as e:
            if effective_routing == "tor_fallback" and proxies:
                try:
                    result = _call({})
                except Exception as e2:
                    return jsonify({"error": f"LLM request failed: {e2}"}), 503
            else:
                return jsonify({"error": f"LLM request failed: {e}"}), 503

        return jsonify({
            "prompt":          str(result.get("prompt", "") or ""),
            "negative_prompt": str(result.get("negative_prompt", "") or ""),
        })


    @app.route("/api/image-gen/generate", methods=["POST"])
    def api_image_gen_generate():
        """Image generation step: send prompt to configured image gen provider."""
        import base64 as _b64
        import requests as _req
        from flask import jsonify
        from urllib.parse import urlparse as _up

        body            = request.get_json(force=True, silent=True) or {}
        prompt          = (body.get("prompt") or "").strip()[:2000]
        negative_prompt = (body.get("negative_prompt") or "").strip()[:500]
        if not prompt:
            return jsonify({"error": "prompt required"}), 400

        prefs           = config.prefs
        provider        = prefs.get("img_gen_provider", "openai").strip()
        base_url        = prefs.get("img_gen_base_url", "").strip().rstrip("/")
        api_key         = prefs.get("img_gen_api_key", "").strip()
        model           = prefs.get("img_gen_model", "dall-e-3").strip()
        size            = prefs.get("img_gen_size", "1024x1024").strip()
        steps           = max(1, min(150, int(prefs.get("img_gen_steps", 20) or 20)))
        cfg             = float(prefs.get("img_gen_cfg_scale", 7.0) or 7.0)
        user_neg        = (prefs.get("img_gen_negative_prompt", "") or "").strip()

        # Merge user-supplied negative prompt with the LLM-generated one
        combined_neg = ", ".join(filter(None, [negative_prompt, user_neg]))

        # Local addresses always bypass Tor
        target_host       = (_up(base_url or "http://localhost").hostname or "localhost").lower()
        effective_routing = "direct" if _PRIVATE_HOST_RE.match(target_host) else prefs.get("ai_routing", "direct")
        proxies = {}
        if effective_routing in ("tor", "tor_fallback"):
            try:
                cred = circuits.get_credential()
                p = f"socks5h://{cred}:x@127.0.0.1:{config.tor_port}"
                proxies = {"http": p, "https": p}
            except Exception:
                if effective_routing != "tor_fallback":
                    return jsonify({"error": "Tor unavailable."}), 503

        hdrs = {"Content-Type": "application/json", "User-Agent": "Scrambler/1.0"}

        try:
            if provider == "automatic1111":
                # AUTOMATIC1111 / Forge / SD.Next — POST /sdapi/v1/txt2img
                host = base_url or "http://127.0.0.1:7860"
                w, h = (size.split("x") + ["512", "512"])[:2]
                payload = {
                    "prompt":          prompt,
                    "negative_prompt": combined_neg,
                    "steps":           steps,
                    "cfg_scale":       cfg,
                    "width":           int(w),
                    "height":          int(h),
                    "sampler_name":    "Euler a",
                    "n_iter":          1,
                    "batch_size":      1,
                }
                resp = _req.post(host + "/sdapi/v1/txt2img",
                                 json=payload, headers=hdrs, proxies=proxies, timeout=120)
                resp.raise_for_status()
                images = resp.json().get("images", [])
                if not images:
                    return jsonify({"error": "No image returned from AUTOMATIC1111"}), 502
                return jsonify({"b64": images[0], "prompt": prompt})

            else:
                # OpenAI-compatible (DALL-E 3, fal.ai, LocalAI, etc.)
                host = base_url or "https://api.openai.com"
                if host.endswith("/v1"):
                    host = host[:-3]
                h = {**hdrs}
                if api_key:
                    h["Authorization"] = f"Bearer {api_key}"
                payload = {
                    "model":           model,
                    "prompt":          prompt,
                    "n":               1,
                    "size":            size,
                    "response_format": "b64_json",
                }
                resp = _req.post(host + "/v1/images/generations",
                                 json=payload, headers=h, proxies=proxies, timeout=120)

                # Some providers (e.g. OpenRouter) don't expose /images/generations
                # but do support image-gen models via /chat/completions.
                if resp.status_code == 404:
                    chat_payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                    resp = _req.post(host + "/v1/chat/completions",
                                     json=chat_payload, headers=h,
                                     proxies=proxies, timeout=120)

                if not resp.ok:
                    try:
                        detail = resp.json()
                        msg = (detail.get("error") or {}).get("message") or str(detail)
                    except Exception:
                        import re as _re
                        msg = _re.sub(r'<[^>]+>', '', resp.text)[:300].strip()
                    return jsonify({"error": f"Image API {resp.status_code}: {msg}"}), 502

                rj = resp.json()

                # Try standard images/generations format first
                b64 = ""
                revised = ""
                if "data" in rj:
                    data = rj["data"][0]
                    b64  = data.get("b64_json") or ""
                    revised = data.get("revised_prompt", "")
                    if not b64 and data.get("url"):
                        img_resp = _req.get(data["url"], timeout=30)
                        img_resp.raise_for_status()
                        b64 = _b64.b64encode(img_resp.content).decode()

                # Try chat/completions format (OpenRouter image models)
                if not b64 and "choices" in rj:
                    import re as _re
                    msg_obj = rj["choices"][0]["message"]
                    # OpenRouter puts images in message.images (non-standard)
                    or_images = msg_obj.get("images") or []
                    for img_block in or_images:
                        url = (img_block.get("image_url") or {}).get("url", "")
                        if url:
                            if url.startswith("data:"):
                                b64 = url.split(",", 1)[-1]
                            else:
                                img_resp = _req.get(url, timeout=30)
                                img_resp.raise_for_status()
                                b64 = _b64.b64encode(img_resp.content).decode()
                        if b64:
                            break
                    content = msg_obj.get("content", "")
                    # Content may be a list of blocks or a plain string
                    if not b64 and isinstance(content, list):
                        for block in content:
                            url = ""
                            if isinstance(block, dict):
                                if block.get("type") == "image_url":
                                    url = (block.get("image_url") or {}).get("url", "")
                                elif block.get("type") == "image":
                                    # base64 block
                                    b64 = block.get("data", "")
                            if url:
                                if url.startswith("data:"):
                                    b64 = url.split(",", 1)[-1]
                                else:
                                    img_resp = _req.get(url, timeout=30)
                                    img_resp.raise_for_status()
                                    b64 = _b64.b64encode(img_resp.content).decode()
                            if b64:
                                break
                    elif isinstance(content, str):
                        # Some providers embed a data URL or image URL in plain text
                        m = _re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', content)
                        if m:
                            b64 = m.group(1)
                        else:
                            m = _re.search(r'https?://\S+\.(?:png|jpg|jpeg|webp|gif)\S*', content, _re.I)
                            if m:
                                img_resp = _req.get(m.group(0), timeout=30)
                                img_resp.raise_for_status()
                                b64 = _b64.b64encode(img_resp.content).decode()

                if not b64:
                    return jsonify({"error": "No image returned by provider"}), 502

                return jsonify({"b64": b64, "prompt": revised or prompt})

        except Exception as e:
            return jsonify({"error": f"Image generation failed: {e}"}), 503


    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            raw = request.form.get("instances", "")
            new_instances = [
                ln.strip() for ln in raw.splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
            save_instances(config_dir / "instances.txt", new_instances)
            config.instances = new_instances
            selector.update_instances(new_instances)
            threading.Thread(
                target=_refresh_engine_native_categories,
                args=(new_instances, _get_routing(config.prefs), config.tor_port),
                daemon=True,
                name="engine-cat-refresh",
            ).start()

            saved_app = config.prefs.get("appearance", {})
            new_appearance = {
                "title":           request.form.get("appearance_title", saved_app.get("title", "Scrambler")).strip() or "Scrambler",
                "subtitle":        request.form.get("appearance_subtitle", saved_app.get("subtitle", "")),
                "tagline":         request.form.get("appearance_tagline", saved_app.get("tagline", "")),
                "favicon":         request.form.get("appearance_favicon", saved_app.get("favicon", "🔍")).strip() or "🔍",
                "font_family":        request.form.get("appearance_font_family", saved_app.get("font_family", "")).strip(),
                "font_import_url":    request.form.get("appearance_font_import_url", saved_app.get("font_import_url", "")).strip(),
                "font_face_css":      request.form.get("appearance_font_face_css", saved_app.get("font_face_css", "")),
                "background_image":   request.form.get("appearance_background_image", saved_app.get("background_image", "")).strip(),
                "background_size":    request.form.get("appearance_background_size", saved_app.get("background_size", "cover")),
                "background_opacity": float(request.form.get("appearance_background_opacity", saved_app.get("background_opacity", 0.15))),
                "background_video_loop": request.form.get("appearance_background_video_loop") == "1",
                "loading_bg_url":        request.form.get("appearance_loading_bg_url", saved_app.get("loading_bg_url", "")).strip(),
                "loading_bg_opacity":    float(request.form.get("appearance_loading_bg_opacity", saved_app.get("loading_bg_opacity", 1.0))),
                "loading_bg_loop":       request.form.get("appearance_loading_bg_loop") == "1",
                "loading_quips": [
                    l.strip() for l in request.form.get("appearance_loading_quips", "").splitlines()
                    if l.strip()
                ],
                "loading_bar_style": request.form.get("appearance_loading_bar_style", saved_app.get("loading_bar_style", "crawl")),
                "loading_indicator": request.form.get("appearance_loading_indicator") if request.form.get("appearance_loading_indicator") in ("bar", "console", "none", "tree", "tracker") else saved_app.get("loading_indicator", "bar"),
                "christmas_tree_style": request.form.get("appearance_christmas_tree_style", saved_app.get("christmas_tree_style", "dots")),
                "tree_icon_loading":      request.form.get("appearance_tree_icon_loading", saved_app.get("tree_icon_loading", "")).strip(),
                "tree_icon_loading_spin": request.form.get("appearance_tree_icon_loading_spin") == "1",
                "tree_icon_ok":           request.form.get("appearance_tree_icon_ok",      saved_app.get("tree_icon_ok",      "")).strip(),
                "tree_icon_fail":         request.form.get("appearance_tree_icon_fail",    saved_app.get("tree_icon_fail",    "")).strip(),
                "background_audio_url":    request.form.get("appearance_background_audio_url", saved_app.get("background_audio_url", "")).strip(),
                "background_audio_volume": float(request.form.get("appearance_background_audio_volume", saved_app.get("background_audio_volume", 0.5))),
                "background_audio_loop":   request.form.get("appearance_background_audio_loop") == "1",
                "cursor_url":       request.form.get("appearance_cursor_url", saved_app.get("cursor_url", "")).strip(),
                "cursor_trail":     request.form.get("appearance_cursor_trail", saved_app.get("cursor_trail", "off")),
                "cursor_trail_url": request.form.get("appearance_cursor_trail_url", saved_app.get("cursor_trail_url", "")).strip(),
                "typing_sound":     request.form.get("appearance_typing_sound", saved_app.get("typing_sound", "off")),
                "typing_sound_url": request.form.get("appearance_typing_sound_url", saved_app.get("typing_sound_url", "")).strip(),
                "search_sound":     request.form.get("appearance_search_sound", saved_app.get("search_sound", "off")),
                "search_sound_url": request.form.get("appearance_search_sound_url", saved_app.get("search_sound_url", "")).strip(),
                "click_sound":      request.form.get("appearance_click_sound", saved_app.get("click_sound", "off")),
                "click_sound_url":  request.form.get("appearance_click_sound_url", saved_app.get("click_sound_url", "")).strip(),
                "sound_volume":     float(request.form.get("appearance_sound_volume", saved_app.get("sound_volume", 0.5))),
                "colors": {
                    var: request.form.get(var, saved_app.get("colors", {}).get(var, DEFAULT_COLORS[var]))
                    for var in DEFAULT_COLORS
                },
            }
            new_prefs = {
                "streaming_results": "live" if request.form.get("appearance_streaming_results") == "live" else "off",
                "language": request.form.get("language", config.prefs["language"]),
                "safesearch": request.form.get("safesearch", config.prefs["safesearch"]),
                "categories": request.form.get("categories", config.prefs["categories"]),
                "engines": ",".join(
                    e.strip() for e in request.form.get("engines", "").splitlines()
                    if e.strip()
                ) or config.prefs["engines"],
                "theme": "simple",
                "multi_instance": int(request.form.get("multi_instance", config.prefs.get("multi_instance", 1))),
                "use_tor": request.form.get("use_tor") if request.form.get("use_tor") in ("tor", "tor_fallback", "direct") else "tor",
                "dearrow": request.form.get("dearrow") == "1",
                "map_tile_provider": request.form.get("map_tile_provider", "openstreetmap") if request.form.get("map_tile_provider") in ("openstreetmap", "carto_light", "carto_dark", "custom", "none") else "openstreetmap",
                "map_tile_routing": request.form.get("map_tile_routing", "tor") if request.form.get("map_tile_routing") in ("tor", "tor_fallback", "direct") else "tor",
                "map_tile_custom_url": request.form.get("map_tile_custom_url", "").strip(),
                "route_provider": request.form.get("route_provider", "osrm_public") if request.form.get("route_provider") in ("osrm_public", "custom") else "osrm_public",
                "route_custom_url": request.form.get("route_custom_url", "").strip(),
                "route_transit_url": request.form.get("route_transit_url", "https://api.transitous.org/otp").strip() or "https://api.transitous.org/otp",
                "transit_navitia_key": request.form.get("transit_navitia_key", "").strip() or config.prefs.get("transit_navitia_key", ""),
                "map_geolocation": request.form.get("map_geolocation", "hybrid") if request.form.get("map_geolocation") in ("off", "low", "high", "hybrid") else "hybrid",
                "map_units": request.form.get("map_units", "metric") if request.form.get("map_units") in ("metric", "imperial") else "metric",
                "map_routing": request.form.get("map_routing", "tor") if request.form.get("map_routing") in ("tor", "tor_fallback", "direct") else "tor",
                "failover_timeout": max(3, min(120, int(request.form.get("failover_timeout", 12) or 12))),
                "map_offline_method": request.form.get("map_offline_method", "none") if request.form.get("map_offline_method") in ("none", "service_worker", "pmtiles", "both") else "none",
                "map_offline_behavior": request.form.get("map_offline_behavior", "hybrid") if request.form.get("map_offline_behavior") in ("hybrid", "offline") else "hybrid",
                "map_pmtiles_url": request.form.get("map_pmtiles_url", "").strip(),
                "custom_dns": request.form.get("custom_dns", "").strip(),
                "weighted_selection": request.form.get("weighted_selection") == "1",
                "result_cache": request.form.get("result_cache") == "1",
                "circuit_warmup": max(0, int(request.form.get("circuit_warmup", 0) or 0)),
                "priority_sources": [
                    s.strip() for s in request.form.get("priority_sources", "").splitlines()
                    if s.strip() and not s.strip().startswith("#")
                ],
                "demote_domains": [
                    s.strip() for s in request.form.get("demote_domains", "").splitlines()
                    if s.strip() and not s.strip().startswith("#")
                ],
                "demote_except_categories": request.form.getlist("demote_except_categories"),
                "priority_source_cap": max(1, int(request.form.get("priority_source_cap", config.prefs.get("priority_source_cap", 1)) or 1)),
                "priority_source_cutoff": max(0.0, min(1.0, float(request.form.get("priority_source_cutoff", config.prefs.get("priority_source_cutoff", 0.0)) or 0.0))),
                "blocked_engines": "\n".join(
                    s.strip() for s in request.form.get("blocked_engines", "").splitlines()
                    if s.strip()
                ),
                "engine_ranking":    request.form.get("engine_ranking") == "1",
                "consensus_ranking": request.form.get("consensus_ranking") == "1",
                "lexical_scoring":    request.form.get("lexical_scoring") == "1",
                "semantic_reranking": request.form.get("semantic_reranking") == "1",
                "semantic_model":     request.form.get("semantic_model", config.prefs.get("semantic_model", "all-MiniLM-L6-v2")).strip() or "all-MiniLM-L6-v2",
                "semantic_cutoff":    max(0.0, min(1.0, float(request.form.get("semantic_cutoff", config.prefs.get("semantic_cutoff", 0.0)) or 0.0))),
                "server_port": int(request.form.get("server_port", config.prefs.get("server_port", 7777))),
                "default_category": request.form.get("default_category", config.prefs.get("default_category", "all")),
                "engine_failover_limit": max(0, int(request.form.get("engine_failover_limit", config.prefs.get("engine_failover_limit", 3)) or 3)),
                "failover_phase_timeout": max(0, min(300, int(request.form.get("failover_phase_timeout", config.prefs.get("failover_phase_timeout", 60)) or 0))),
                "direct_engine_fallback": request.form.get("direct_engine_fallback") == "1",
                "brave_api_key": request.form.get("brave_api_key", "").strip() or config.prefs.get("brave_api_key", ""),
                "domain_cap": max(0, int(request.form.get("domain_cap", config.prefs.get("domain_cap", 3)) or 0)),
                "freshness_boost": request.form.get("freshness_boost") == "1",
                "fuzzy_dedup": request.form.get("fuzzy_dedup") == "1",
                "reader_mode": request.form.get("reader_mode", "off") if request.form.get("reader_mode") in ("off", "both", "reader") else "off",
                "thumbnail_proxy": request.form.get("thumbnail_proxy", "direct") if request.form.get("thumbnail_proxy") in ("direct", "tor", "tor_fallback") else "direct",
                "tab_categories": _parse_tab_categories(request.form.get("tab_categories", "")),
                "autopick_schedule": request.form.get("autopick_schedule", config.prefs.get("autopick_schedule", "never")),
                "autopick_interval": max(1, int(request.form.get("autopick_interval", config.prefs.get("autopick_interval", 60)) or 60)),
                "autopick_count":    max(1, min(100, int(request.form.get("autopick_count", config.prefs.get("autopick_count", 5)) or 5))),
                "autopick_min_engines": max(0, int(request.form.get("autopick_min_engines", config.prefs.get("autopick_min_engines", 5)) or 0)),
                "ai_provider": request.form.get("ai_provider", "") if request.form.get("ai_provider") in ("", "anthropic", "openai_compat", "ollama") else "",
                "ai_api_key": request.form.get("ai_api_key", "").strip() or config.prefs.get("ai_api_key", ""),
                "jina_api_key": request.form.get("jina_api_key", "").strip() or config.prefs.get("jina_api_key", ""),
                "ai_base_url": request.form.get("ai_base_url", "").strip(),
                "ai_model": request.form.get("ai_model", "").strip(),
                "ai_routing": request.form.get("ai_routing", "direct") if request.form.get("ai_routing") in ("direct", "tor", "tor_fallback") else "direct",
                "ai_max_results": max(1, min(20, int(request.form.get("ai_max_results", 10) or 10))),
                "ai_use_jina": request.form.get("ai_use_jina") == "1",
                "jina_routing": request.form.get("jina_routing", "direct") if request.form.get("jina_routing") in ("direct", "tor", "tor_fallback") else "direct",
                "jina_reader": request.form.get("jina_reader") == "1",
                "ai_system_prompt": request.form.get("ai_system_prompt", "").strip(),
                "ai_avatar": request.form.get("ai_avatar", "").strip(),
                "ai_user_avatar": request.form.get("ai_user_avatar", "").strip(),
                "ai_reranking": request.form.get("ai_reranking") == "1",
                "ai_rerank_count": max(1, min(100, int(request.form.get("ai_rerank_count", 20) or 20))),
                "ai_rerank_timing": request.form.get("ai_rerank_timing", "final") if request.form.get("ai_rerank_timing") in ("final", "streaming", "ask_only") else "final",
                "ai_rerank_on_ask": request.form.get("ai_rerank_on_ask") == "1",
                "ai_rerank_use_search_ai": request.form.get("ai_rerank_use_search_ai") == "1",
                "ai_rerank_provider": request.form.get("ai_rerank_provider", "") if request.form.get("ai_rerank_provider") in ("", "anthropic", "openai_compat", "ollama", "rerank_api") else "",
                "ai_rerank_api_key": request.form.get("ai_rerank_api_key", "").strip() or config.prefs.get("ai_rerank_api_key", ""),
                "ai_rerank_model": request.form.get("ai_rerank_model", "").strip(),
                "ai_rerank_base_url": request.form.get("ai_rerank_base_url", "").strip(),
                "ai_rerank_routing": request.form.get("ai_rerank_routing", "direct") if request.form.get("ai_rerank_routing") in ("direct", "tor", "tor_fallback") else "direct",
                "img_gen_provider":        request.form.get("img_gen_provider", "off") if request.form.get("img_gen_provider") in ("off", "openai", "automatic1111") else "off",
                "img_gen_base_url":        request.form.get("img_gen_base_url", "").strip(),
                "img_gen_api_key":         request.form.get("img_gen_api_key", "").strip() or config.prefs.get("img_gen_api_key", ""),
                "img_gen_model":           request.form.get("img_gen_model", "").strip(),
                "img_gen_size":            request.form.get("img_gen_size", "1024x1024").strip(),
                "img_gen_steps":           max(1, min(150, int(request.form.get("img_gen_steps", 20) or 20))),
                "img_gen_cfg_scale":       max(1.0, min(30.0, float(request.form.get("img_gen_cfg_scale", 7.0) or 7.0))),
                "img_gen_negative_prompt": request.form.get("img_gen_negative_prompt", "").strip(),
                "appearance": new_appearance,
            }
            save_prefs(config_dir / "preferences.json", new_prefs)
            config.prefs.update(new_prefs)
            profile_save_name = request.form.get("profile_save_name", "").strip()
            if profile_save_name:
                from .config import load_search_profiles, save_search_profiles, PROFILE_PREFS
                profiles = load_search_profiles(config_dir / "search_profiles.json")
                profiles[profile_save_name] = {k: config.prefs[k] for k in PROFILE_PREFS if k in config.prefs}
                save_search_profiles(config_dir / "search_profiles.json", profiles)
                config.prefs["active_profile"] = profile_save_name
                config.prefs["settings_customized"] = False
            else:
                config.prefs["active_profile"] = ""
                config.prefs["settings_customized"] = True
            save_prefs(config_dir / "preferences.json", config.prefs)
            selector.weighted = new_prefs["weighted_selection"]
            _cw = int(new_prefs.get("circuit_warmup", 0))
            if _cw > 0 and _get_routing(new_prefs) != "direct":
                circuits.start(config.tor_port, pool_size=_cw)
            else:
                circuits.stop()
            # wake scheduler so it picks up any schedule/interval change immediately
            _ap_wake.set()
            return redirect(url_for("settings"))

        from .ranker import semantic_available, install_status
        return render_template(
            "settings.html",
            instances_text="\n".join(config.instances),
            prefs=config.prefs,
            instance_count=len(config.instances),
            priority_sources_text="\n".join(config.prefs.get("priority_sources") or []),
            server_port=config.prefs.get("server_port", 7777),
            semantic_available=semantic_available(),
            sem_install=install_status(),
        )

    return app


# ── Engine selection ─────────────────────────────────────────────────────────

# Regex to parse user-annotated engine riders: "youtube [videos]"
_ANN_RE = re.compile(r'^(.*?)\s*\[([^\]]+)\]\s*$')

# General engines that SearXNG ships with category-specific variants.
# Key: engine name users type; value: {category: searxng_engine_name}
# SearXNG engine name → bang shortcut.  Used to inject bangs into the query as a
# second engine-selection mechanism alongside engines= URL params: locked instances
# ignore the URL param but still honour text bangs parsed from the query.
# Confirmed working on tested instances; unknown shortcuts are simply omitted so
# the URL param remains the only selector for those engines.
_ENGINE_BANGS: dict[str, str] = {
    # General web
    "bing":             "!bi",
    "google":           "!g",
    "duckduckgo":       "!ddg",
    "startpage":        "!sp",
    "brave":            "!brave",
    "qwant":            "!qw",
    "mojeek":           "!moj",
    "marginalia":       "!mar",
    "mwmbl":            "!mwmbl",
    "yandex":           "!ya",
    # News variants (remapped by _select_engines_for_category)
    "bing news":        "!bin",
    "google news":      "!gon",
    "duckduckgo news":  "!ddn",
    "brave.news":       "!brn",
    "qwant news":       "!qwn",
    "yandex news":      "!yan",
    # Reference / encyclopaedia
    "wikipedia":        "!wp",
    "wikidata":         "!wd",
    "wikinews":         "!wn",
    "wikibooks":        "!wb",
    "wikiquote":        "!wq",
    "wikisource":       "!ws",
    "wikiversity":      "!wv",
    # Science / academic
    "arxiv":            "!arx",
    "pubmed":           "!pm",
    "semantic scholar": "!se",
    "crossref":         "!cr",
    # Code / IT
    "github":           "!gh",
    "stackoverflow":    "!st",
    "gitlab":           "!gl",
    # Social / community
    "reddit":           "!re",
    "lemmy posts":      "!lp",
    # Images
    "bing images":      "!bii",
    "google images":    "!goi",
    "duckduckgo images": "!ddi",
    "flickr":           "!fl",
    "unsplash":         "!us",
    "imgur":            "!img",
    # Video
    "youtube":          "!yt",
    "invidious":        "!inv",
    # Maps
    "openstreetmap":    "!osm",
}

_GENERAL_ENGINE_VARIANTS: dict[str, dict[str, str]] = {
    "bing":       {"images": "bing images",       "videos": "bing videos",       "news": "bing news"},
    "google":     {"images": "google images",     "videos": "google videos",     "news": "google news"},
    "duckduckgo": {"images": "duckduckgo images", "videos": "duckduckgo videos", "news": "duckduckgo news"},
    "brave":      {"images": "brave.images",      "videos": "brave.videos",      "news": "brave.news"},
    "qwant":      {"images": "qwant images",      "videos": "qwant videos",      "news": "qwant news"},
    "yandex":     {"images": "yandex images",     "videos": "yandex videos",     "news": "yandex news"},
}

# Engines that are native to a specific category and shouldn't bleed into others.
# Covers the full SearXNG engine catalogue (~200 engines). General-purpose web
# engines (bing, google, duckduckgo, etc.) are intentionally absent — they are
# handled via _GENERAL_ENGINE_VARIANTS and should not be treated as foreign in
# any focused category.
_CATEGORY_NATIVE_ENGINES: dict[str, frozenset] = {
    "images": frozenset([
        # General-engine image variants
        "bing images", "google images", "duckduckgo images", "brave.images",
        "qwant images", "yandex images", "yahoo images", "seekr images",
        # Photo / stock / art platforms
        "flickr", "unsplash", "deviantart", "openverse", "imgur", "wallhaven",
        "pexels", "pixabay", "500px", "svgrepo", "stockio",
        # Anime / illustration boards
        "danbooru", "gelbooru", "safebooru", "konachan", "yande.re", "zerochan",
        "lolibooru", "rule34", "e621", "e926",
        # Wiki media
        "wikicommons",
        # Misc
        "frinkiac", "reisagashi",
    ]),
    "videos": frozenset([
        # General-engine video variants
        "bing videos", "google videos", "duckduckgo videos", "brave.videos",
        "qwant videos", "yandex videos",
        # Video platforms
        "youtube", "invidious", "piped", "viewtube",
        "vimeo", "dailymotion", "odysee", "rumble", "bilibili",
        "peertube", "sepiasearch",
        "public domain videos", "public_domain_videos",
        "mediathekviewweb",
    ]),
    "music": frozenset([
        "bandcamp", "genius", "soundcloud", "radio browser",
        "mixcloud", "last.fm", "lastfm", "musicbrainz",
        "deezer", "jamendo",
    ]),
    "files": frozenset([
        # Torrent trackers / indexes
        "pirate bay", "nyaa", "tokyotoshokan", "1337x", "btdigg",
        "kickass torrents", "kickasstorrents", "bt4g", "torrentgalaxy",
        "magnetdl", "snowfl", "solidtorrents", "bittorrent.am",
        "digbt", "ext_torrent", "btmet", "torrentproject",
        # Book / document archives
        "annas archive", "openlibrary", "library genesis", "libgen",
        "mango", "pdfdrive", "freepdf", "z-library",
        # Wiki media files
        "wikicommons.files",
    ]),
    "map": frozenset([
        "openstreetmap", "photon",
    ]),
    "news": frozenset([
        # General-engine news variants
        "bing news", "google news", "duckduckgo news", "brave.news",
        "qwant news", "yandex news",
        # Wire services & broadcasters
        "reuters", "bbc news", "voa news",
        # Aggregators
        "wikinews", "techmeme", "currents api", "currentsapi",
        # Regional press
        "the guardian", "lemonde", "tagesschau", "taz", "heise",
        "golem", "commoncrawl news",
    ]),
    "science": frozenset([
        # Pre-print / journals
        "arxiv", "pubmed", "semantic scholar", "crossref",
        "base", "core", "scihub",
        # Open-access repositories
        "openairedatasets", "openaire", "openairepublications",
        "unpaywall", "europe pmc", "fatcat scholar",
        # Specialised
        "google scholar", "pdbe", "doaj", "dimensions",
        "biorxiv", "medrxiv", "chemrxiv",
    ]),
    "it": frozenset([
        # Code hosting
        "github", "gitlab", "bitbucket", "codeberg", "sourcehut",
        # Q&A / knowledge
        "stackoverflow", "mdn",
        # Package registries
        "pkg.go.dev", "crates.io", "npm", "pypi",
        "hex.pm", "rubygems", "packagist", "nuget",
        "docker hub", "dockerhub", "repology",
        # Code search
        "searchcode",
        # Community
        "hacker news", "hackernews",
    ]),
    "social media": frozenset([
        # Link aggregators
        "reddit", "hacker news", "hackernews",
        "lobste.rs", "lobsters", "tildes",
        # Fediverse
        "lemmy", "mastodon", "peertube", "fediverse",
        # Micro-blogging
        "twitter", "nostr",
        # Boards / chans
        "4chan",
    ]),
}

# Domain → category for category-specific websites.
# Used as a fallback when a result has no engine attribution (or only general
# engines) so that e.g. a github.com result never bleeds into the News tab.
# Suffix-matched: "github.com" covers "gist.github.com", "raw.githubusercontent.com", etc.
# General-purpose sites (nytimes.com, bbc.com, etc.) are intentionally absent —
# unknown domains are treated as general and are never blocked from any tab.
# NOTE: github.io / gitlab.io are excluded — they host arbitrary user pages.
_DOMAIN_NATIVE_CATEGORY: dict[str, str] = {
    # ── IT / Code hosting ────────────────────────────────────────────────────
    "github.com":               "it",
    "githubusercontent.com":    "it",
    "gitlab.com":               "it",
    "bitbucket.org":            "it",
    "codeberg.org":             "it",
    "sr.ht":                    "it",       # sourcehut
    "sourceforge.net":          "it",
    # Container / image registries
    "hub.docker.com":           "it",
    "quay.io":                  "it",
    "ghcr.io":                  "it",
    # Q&A / documentation
    "stackoverflow.com":        "it",
    "stackexchange.com":        "it",       # covers unix.SE, physics.SE, etc.
    "superuser.com":            "it",
    "serverfault.com":          "it",
    "askubuntu.com":            "it",
    "developer.mozilla.org":    "it",
    "docs.microsoft.com":       "it",
    "learn.microsoft.com":      "it",
    "developer.apple.com":      "it",
    "developer.android.com":    "it",
    "docs.python.org":          "it",
    "readthedocs.io":           "it",
    "readthedocs.org":          "it",
    # Package registries
    "npmjs.com":                "it",
    "crates.io":                "it",
    "pypi.org":                 "it",
    "pkg.go.dev":               "it",
    "rubygems.org":             "it",
    "hex.pm":                   "it",
    "packagist.org":            "it",
    "nuget.org":                "it",
    "hackage.haskell.org":      "it",
    "pub.dev":                  "it",       # Dart / Flutter
    "clojars.org":              "it",
    "cocoapods.org":            "it",
    "anaconda.org":             "it",
    "formulae.brew.sh":         "it",
    "search.maven.org":         "it",
    "mvnrepository.com":        "it",
    "docs.rs":                  "it",
    "lib.rs":                   "it",
    # Distro / package tracking
    "repology.org":             "it",
    "packages.debian.org":      "it",
    "aur.archlinux.org":        "it",
    "freshports.org":           "it",
    "search.nixos.org":         "it",
    "launchpad.net":            "it",
    # Code search / misc
    "searchcode.com":           "it",
    "grep.app":                 "it",
    "openhub.net":              "it",
    "codeproject.com":          "it",
    "kubernetes.io":            "it",
    "kernel.org":               "it",

    # ── Science / Academic ───────────────────────────────────────────────────
    # Pre-prints & repositories
    "arxiv.org":                "science",
    "biorxiv.org":              "science",
    "medrxiv.org":              "science",
    "chemrxiv.org":             "science",
    "psyarxiv.com":             "science",
    "ssrn.com":                 "science",
    "osf.io":                   "science",
    "zenodo.org":               "science",
    "figshare.com":             "science",
    "datadryad.org":            "science",
    "openalex.org":             "science",
    # Indexes & databases
    "ncbi.nlm.nih.gov":         "science",
    "europepmc.org":            "science",
    "semanticscholar.org":      "science",
    "crossref.org":             "science",
    "doi.org":                  "science",
    "scholar.google.com":       "science",
    "base-search.net":          "science",
    "core.ac.uk":               "science",
    "unpaywall.org":            "science",
    "doaj.org":                 "science",
    "dimensions.ai":            "science",
    "fatcat.wiki":              "science",
    "rcsb.org":                 "science",
    "researchgate.net":         "science",
    "academia.edu":             "science",
    # Publishers (full-text access signals academic content)
    "ieeexplore.ieee.org":      "science",
    "dl.acm.org":               "science",
    "jstor.org":                "science",
    "onlinelibrary.wiley.com":  "science",
    "link.springer.com":        "science",
    "sciencedirect.com":        "science",
    "pnas.org":                 "science",
    "plos.org":                 "science",
    "science.org":              "science",

    # ── Social Media ─────────────────────────────────────────────────────────
    "reddit.com":               "social media",
    "redd.it":                  "social media",
    "twitter.com":              "social media",
    "x.com":                    "social media",
    "nitter.net":               "social media",
    "bsky.app":                 "social media",
    "4chan.org":                "social media",
    "4channel.org":             "social media",
    "lobste.rs":                "social media",
    "tildes.net":               "social media",
    "news.ycombinator.com":     "social media",
    "nostr.com":                "social media",
    # Lemmy instances (fediverse link-aggregator)
    "lemmy.world":              "social media",
    "lemmy.ml":                 "social media",
    "lemmy.blahaj.zone":        "social media",
    "beehaw.org":               "social media",
    "sh.itjust.works":          "social media",
    "programming.dev":          "social media",
    "aussie.zone":              "social media",
    "lemmy.sdf.org":            "social media",
    "feddit.de":                "social media",
    "feddit.nl":                "social media",
    "feddit.uk":                "social media",
    "lemdro.id":                "social media",
    "lemmy.ca":                 "social media",
    "lemm.ee":                  "social media",
    "lemmynsfw.com":            "social media",
    # Kbin / Mbin (Fediverse magazine-style)
    "kbin.social":              "social media",
    "mbin.earth":               "social media",
    "piefed.social":            "social media",
    # Mastodon instances
    "mastodon.social":          "social media",
    "fosstodon.org":            "social media",
    "infosec.exchange":         "social media",
    "hachyderm.io":             "social media",
    "mas.to":                   "social media",
    "universeodon.com":         "social media",
    "sigmoid.social":           "social media",
    "techhub.social":           "social media",
    "social.coop":              "social media",
    "mathstodon.xyz":           "social media",
    "chaos.social":             "social media",
    "octodon.social":           "social media",

    # ── Files / Torrents ─────────────────────────────────────────────────────
    "thepiratebay.org":         "files",
    "1337x.to":                 "files",
    "bt4g.com":                 "files",
    "torrentgalaxy.to":         "files",
    "nyaa.si":                  "files",
    "annas-archive.org":        "files",
    "libgen.is":                "files",
    "libgen.rs":                "files",
    "libgen.li":                "files",
    "library.lol":              "files",
    "z-lib.org":                "files",
    "zlibrary.to":              "files",
    "openlibrary.org":          "files",
    "btdig.com":                "files",
    "magnetdl.com":             "files",
    "snowfl.com":               "files",
    "solidtorrents.to":         "files",
    "kickasstorrents.to":       "files",
    "limetorrents.info":        "files",
    "torrentz2.eu":             "files",
    "eztv.re":                  "files",
    "yts.mx":                   "files",
    "rutracker.org":            "files",
    "pdfdrive.com":             "files",
    "manybooks.net":            "files",
    "standardebooks.org":       "files",
    "gutenberg.org":            "files",

    # ── News ─────────────────────────────────────────────────────────────────
    # Wire services
    "reuters.com":              "news",
    "apnews.com":               "news",
    "afp.com":                  "news",
    # US national / regional
    "nytimes.com":              "news",
    "washingtonpost.com":       "news",
    "wsj.com":                  "news",
    "usatoday.com":             "news",
    "latimes.com":              "news",
    "nypost.com":               "news",
    "chicagotribune.com":       "news",
    "bostonglobe.com":          "news",
    "sfchronicle.com":          "news",
    "seattletimes.com":         "news",
    "denverpost.com":           "news",
    "dallasnews.com":           "news",
    "miamiherald.com":          "news",
    "azcentral.com":            "news",
    "startribune.com":          "news",
    "thedailybeast.com":        "news",
    "thehill.com":              "news",
    "politico.com":             "news",
    "axios.com":                "news",
    "npr.org":                  "news",
    # US broadcast
    "cnn.com":                  "news",
    "foxnews.com":              "news",
    "msnbc.com":                "news",
    "nbcnews.com":              "news",
    "cbsnews.com":              "news",
    "abcnews.go.com":           "news",
    "pbs.org":                  "news",
    # UK
    "bbc.com":                  "news",
    "bbc.co.uk":                "news",
    "theguardian.com":          "news",
    "telegraph.co.uk":          "news",
    "independent.co.uk":        "news",
    "ft.com":                   "news",
    "thetimes.co.uk":           "news",
    "dailymail.co.uk":          "news",
    "mirror.co.uk":             "news",
    "express.co.uk":            "news",
    "thesun.co.uk":             "news",
    "sky.com":                  "news",
    "inews.co.uk":              "news",
    # International
    "aljazeera.com":            "news",
    "dw.com":                   "news",
    "france24.com":             "news",
    "rfi.fr":                   "news",
    "rte.ie":                   "news",
    "cbc.ca":                   "news",
    "globeandmail.com":         "news",
    "nationalpost.com":         "news",
    "abc.net.au":               "news",
    "smh.com.au":               "news",
    "theage.com.au":            "news",
    "nzherald.co.nz":           "news",
    "irishtimes.com":           "news",
    "thejournal.ie":            "news",
    "lemonde.fr":               "news",
    "lefigaro.fr":              "news",
    "liberation.fr":            "news",
    "spiegel.de":               "news",
    "faz.net":                  "news",
    "sueddeutsche.de":          "news",
    "zeit.de":                  "news",
    "tagesschau.de":            "news",
    "heise.de":                 "news",
    "elpais.com":               "news",
    "elmundo.es":               "news",
    "corriere.it":              "news",
    "repubblica.it":            "news",
    "nrc.nl":                   "news",
    "volkskrant.nl":            "news",
    "svt.se":                   "news",
    "dn.se":                    "news",
    "aftenposten.no":           "news",
    "vg.no":                    "news",
    "hs.fi":                    "news",
    "yle.fi":                   "news",
    "thehindu.com":             "news",
    "ndtv.com":                 "news",
    "hindustantimes.com":       "news",
    "timesofindia.indiatimes.com": "news",
    "dawn.com":                 "news",
    "haaretz.com":              "news",
    "jpost.com":                "news",
    "timesofisrael.com":        "news",
    "scmp.com":                 "news",
    "straitstimes.com":         "news",
    "japantimes.co.jp":         "news",
    "mainichi.jp":              "news",
    "businessinsider.com":      "news",
    # Business / finance news
    "bloomberg.com":            "news",
    "cnbc.com":                 "news",
    "marketwatch.com":          "news",
    "fortune.com":              "news",
    "forbes.com":               "news",
    "barrons.com":              "news",
    # Long-form / magazines with news output
    "economist.com":            "news",
    "theatlantic.com":          "news",
    "newyorker.com":            "news",
    "vox.com":                  "news",
    "slate.com":                "news",
    "salon.com":                "news",
    "huffpost.com":             "news",
    "motherjones.com":          "news",
    "jacobin.com":              "news",
    "newrepublic.com":          "news",
    "thenation.com":            "news",
    "reason.com":               "news",
    # Tech news
    "techcrunch.com":           "news",
    "theverge.com":             "news",
    "arstechnica.com":          "news",
    "wired.com":                "news",
    "engadget.com":             "news",
    "zdnet.com":                "news",
    "cnet.com":                 "news",
    "venturebeat.com":          "news",
    "gizmodo.com":              "news",
    "techradar.com":            "news",
    "tomshardware.com":         "news",
    "anandtech.com":            "news",
    "extremetech.com":          "news",
    "pcmag.com":                "news",
    "9to5mac.com":              "news",
    "macrumors.com":            "news",
    "appleinsider.com":         "news",
    "9to5google.com":           "news",
    "androidpolice.com":        "news",
    "xda-developers.com":       "news",
    "bleepingcomputer.com":     "news",
    "theregister.com":          "news",
    "infoq.com":                "news",
    # Science news (popular science, not journals)
    "scientificamerican.com":   "news",
    "newscientist.com":         "news",
    "sciencenews.org":          "news",
    "quantamagazine.org":       "news",
    "spectrum.ieee.org":        "news",
    "sciencedaily.com":         "news",
    "phys.org":                 "news",
    "space.com":                "news",
    "livescience.com":          "news",
    "popularmechanics.com":     "news",
    # Music journalism / charts
    "pitchfork.com":            "news",
    "nme.com":                  "news",
    "billboard.com":            "news",
    "rollingstone.com":         "news",
    "loudwire.com":             "news",
    "stereogum.com":            "news",
    "consequence.net":          "news",
    "spin.com":                 "news",
    "kerrang.com":              "news",
    "metalinjection.net":       "news",
    "blabbermouth.net":         "news",
    # Gaming journalism
    "ign.com":                  "news",
    "gamespot.com":             "news",
    "kotaku.com":               "news",
    "polygon.com":              "news",
    "pcgamer.com":              "news",
    "eurogamer.net":            "news",
    "rockpapershotgun.com":     "news",
    "gamedeveloper.com":        "news",
    # Film / TV journalism
    "variety.com":              "news",
    "hollywoodreporter.com":    "news",
    "deadline.com":             "news",
    "indiewire.com":            "news",
    "screendaily.com":          "news",
    "empireonline.com":         "news",
    "rottentomatoes.com":       "news",
    # Sports journalism
    "espn.com":                 "news",
    "bleacherreport.com":       "news",
    "theathletic.com":          "news",
    "si.com":                   "news",
    "skysports.com":            "news",
    "goal.com":                 "news",
    "bbc.com/sport":            "news",
    # Aggregators
    "techmeme.com":             "news",
    "ground.news":              "news",
    "wikinews.org":             "news",
    "slashdot.org":             "news",

    # ── Map ──────────────────────────────────────────────────────────────────
    "openstreetmap.org":        "map",
    "osm.org":                  "map",

    # ── Music ────────────────────────────────────────────────────────────────
    # Streaming / hosting
    "bandcamp.com":             "music",
    "soundcloud.com":           "music",
    "mixcloud.com":             "music",
    "deezer.com":               "music",
    "jamendo.com":              "music",
    "open.spotify.com":         "music",
    "tidal.com":                "music",
    "freemusicarchive.org":     "music",
    "ccmixter.org":             "music",
    # Lyrics
    "genius.com":               "music",
    "azlyrics.com":             "music",
    "musixmatch.com":           "music",
    "metrolyrics.com":          "music",
    "songlyrics.com":           "music",
    "lyricsfreak.com":          "music",
    "songmeanings.com":         "music",
    "lyrics.com":               "music",
    # Databases / discovery
    "musicbrainz.org":          "music",
    "last.fm":                  "music",
    "discogs.com":              "music",
    "allmusic.com":             "music",
    "rateyourmusic.com":        "music",
    "whosampled.com":           "music",
    "setlist.fm":               "music",
    "metal-archives.com":       "music",
    "spirit-of-metal.com":      "music",
    # Concerts (music-specific event listings)
    "songkick.com":             "music",
    "bandsintown.com":          "music",
    # Sheet music
    "musescore.com":            "music",
    "imslp.org":                "music",
    "8notes.com":               "music",
    # Radio
    "radio-browser.info":       "music",
    "radio.garden":             "music",
    "shoutcast.com":            "music",

    # ── Videos ───────────────────────────────────────────────────────────────
    "youtube.com":              "videos",
    "youtu.be":                 "videos",
    "vimeo.com":                "videos",
    "dailymotion.com":          "videos",
    "odysee.com":               "videos",
    "rumble.com":               "videos",
    "bilibili.com":             "videos",
    "twitch.tv":                "videos",
    "peertube.tv":              "videos",
    "framatube.org":            "videos",
    "tilvids.com":              "videos",
    "sepiasearch.org":          "videos",
    "diode.zone":               "videos",
}


def _domain_native_cat(url: str) -> str | None:
    """Return the native category for a URL based on its domain, or None.
    Suffix-matches so 'gist.github.com' hits the 'github.com' entry."""
    try:
        from urllib.parse import urlparse as _up
        netloc = _up(url).netloc.lower().split(":")[0]  # strip port
        if netloc in _DOMAIN_NATIVE_CATEGORY:
            return _DOMAIN_NATIVE_CATEGORY[netloc]
        for domain, cat in _DOMAIN_NATIVE_CATEGORY.items():
            if netloc.endswith("." + domain):
                return cat
    except Exception:
        pass
    return None


# Categories that return non-text results — general engines can't usefully be
# remapped to these; without explicit configuration we let SearXNG decide.
_PURE_MEDIA_CATS = frozenset({"images", "videos", "music", "files", "map"})


def _engine_in_native(engine_name: str, native: frozenset) -> bool:
    """Match engine names including sub-engine variants (e.g. 'lemmy comments' matches 'lemmy')."""
    name = engine_name.lower()
    return name in native or any(name.startswith(n + " ") for n in native)

# Inverse mapping: native engine name → its primary category.
_ENGINE_NATIVE_CATEGORY: dict[str, str] = {
    e: cat
    for cat, engines in _CATEGORY_NATIVE_ENGINES.items()
    for e in engines
}


def _primary_cat_of_engine(engine_name: str) -> str | None:
    """Return the category this engine is native to, or None for general engines.
    Handles sub-engine variants like 'lemmy comments' → 'social media'."""
    name = engine_name.lower()
    if name in _ENGINE_NATIVE_CATEGORY:
        return _ENGINE_NATIVE_CATEGORY[name]
    for e, cat in _ENGINE_NATIVE_CATEGORY.items():
        if name.startswith(e + " "):
            return cat
    return None


def _result_is_foreign(r, target_cat: str) -> bool:
    """True when this result demonstrably belongs to a different category.

    Two-stage check:
    1. Engine attribution — if every non-cached engine is positively mapped to a
       different category, the result is foreign.
    2. URL domain fallback — used when engines are absent, cached-only, or all
       general-purpose (no primary category).  Catches untagged results like a
       github.com link that slips into the News tab on the strength of r.date.
    """
    non_cached = [e for e in r.engines if e.lower() != "cached"]
    if non_cached:
        all_other = True
        for e in non_cached:
            pc = _primary_cat_of_engine(e)
            if pc is None or pc == target_cat:
                all_other = False
                break
        if all_other:
            return True
        # At least one engine is general or belongs here — engine check inconclusive;
        # fall through to domain check so e.g. duckduckgo→github.com still gets caught.

    dc = _domain_native_cat(getattr(r, "url", "") or "")
    return dc is not None and dc != target_cat


# SearXNG category labels that mean "general purpose" — not specific enough to use
# as a primary category in our native-engine map.
_GENERIC_SEARXNG_CATS = frozenset({"general", "web", "other"})


def _fetch_instance_category_map(instance_url: str, routing: str, tor_port: int) -> dict[str, str]:
    """GET /config from one SearXNG instance, return {engine_name: primary_category}.
    Silently returns {} on any error."""
    import json as _json
    from urllib.parse import urlparse as _up
    try:
        result = fetch(
            _up(instance_url).scheme + "://" + _up(instance_url).netloc + "/config",
            {},
            tor_port=tor_port,
            timeout=8,
            routing=routing,
        )
        if not result.ok or not result.text:
            return {}
        data = _json.loads(result.text)
        mapping: dict[str, str] = {}
        for engine in data.get("engines", []):
            name = (engine.get("name") or "").strip().lower()
            if not name:
                continue
            cats = [c.lower() for c in (engine.get("categories") or []) if c]
            # Pick the first category that is both specific (non-generic) and known to us
            specific = [c for c in cats if c not in _GENERIC_SEARXNG_CATS and c in _VALID_CATS]
            if specific:
                mapping[name] = specific[0]
        return mapping
    except Exception:
        return {}


def _refresh_engine_native_categories(instances: list[str], routing: str, tor_port: int) -> None:
    """Background startup task: fetch /config from all instances and merge engine→category
    mappings into _ENGINE_NATIVE_CATEGORY, supplementing the hardcoded baseline."""
    import concurrent.futures
    from urllib.parse import urlparse as _up
    # Deduplicate by netloc so mirrored instances don't cause redundant fetches
    seen: set[str] = set()
    unique: list[str] = []
    for url in instances:
        host = _up(url).netloc
        if host and host not in seen:
            seen.add(host)
            unique.append(url)
    if not unique:
        return
    merged: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(unique), 5)) as pool:
        futures = {pool.submit(_fetch_instance_category_map, url, routing, tor_port): url
                   for url in unique}
        for f in concurrent.futures.as_completed(futures, timeout=30):
            try:
                merged.update(f.result())
            except Exception:
                pass
    if merged:
        # Only add truly new entries — never overwrite the hardcoded baseline
        new_keys = {k: v for k, v in merged.items() if k not in _ENGINE_NATIVE_CATEGORY}
        _ENGINE_NATIVE_CATEGORY.update(new_keys)
        print(f"[engine-cats] {len(new_keys)} new engine→category mappings from {len(unique)} instance(s)")


# All engines that are native to any pure-media category.
_ALL_PURE_MEDIA_NATIVE: frozenset = frozenset(
    e for cat in _PURE_MEDIA_CATS for e in _CATEGORY_NATIVE_ENGINES.get(cat, frozenset())
)


def _parse_engine_annotations(engines_str: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split "bing, youtube [videos], bing images [images]" into:
      annotated = [("youtube", "videos"), ("bing images", "images")]
      unannotated = ["bing"]
    """
    annotated: list[tuple[str, str]] = []
    unannotated: list[str] = []
    for raw in engines_str.split(","):
        raw = raw.strip()
        if not raw:
            continue
        m = _ANN_RE.match(raw)
        if m:
            name = m.group(1).strip().lower()
            cat  = m.group(2).strip().lower()
            if name:
                annotated.append((name, cat))
        else:
            unannotated.append(raw.lower())
    return annotated, unannotated


def _select_engines_for_category(engines_str: str, category: str) -> str:
    """Return the engine list appropriate for the given search category.

    Pure media categories (images, videos, music, files, map):
      • [category] riders → included (user-specified, always wins)
      • Engines already native to this category → included (e.g. "youtube" for videos)
      • Everything else → dropped
      • Empty result → caller omits engines= so SearXNG uses instance defaults

    Text categories (general, news, science, it, social media):
      • [category] riders → included first
      • General engines with a known variant for this category → remapped
        (e.g. "bing" → "bing news" for cat=news)
      • Engines native to pure-media categories → dropped (youtube in general tab)
      • Unknown engines → kept (let SearXNG decide)
    """
    if not engines_str or not category:
        return engines_str or ""

    cat = category.lower()
    annotated, unannotated = _parse_engine_annotations(engines_str)

    result: list[str] = []
    seen: set[str] = set()
    cat_native = _CATEGORY_NATIVE_ENGINES.get(cat, frozenset())

    if cat in _PURE_MEDIA_CATS:
        # Explicit riders for this category
        for name, ann_cat in annotated:
            if ann_cat == cat and name not in seen:
                result.append(name)
                seen.add(name)
        # Unannotated engines that are already this category's native type
        for engine in unannotated:
            if engine in cat_native and engine not in seen:
                result.append(engine)
                seen.add(engine)
        # General engines (bing, duckduckgo…) are intentionally NOT remapped here.
        # Their category variants (bing videos, etc.) are rarely enabled on public
        # instances and cause SearXNG to fall back to general results anyway.
        # An empty return lets SearXNG use whatever native engines it has enabled.
        return ",".join(result)

    # Text category — remap and filter
    for name, ann_cat in annotated:
        if ann_cat == cat and name not in seen:
            result.append(name)
            seen.add(name)

    for engine in unannotated:
        if engine in seen:
            continue
        if engine in cat_native:
            result.append(engine)
            seen.add(engine)
        elif engine in _ALL_PURE_MEDIA_NATIVE:
            if cat == "all":
                result.append(engine)  # "all" searches include media engines as-is
                seen.add(engine)
            # else: pure-media engine — useless for focused text category, drop
        elif engine in _GENERAL_ENGINE_VARIANTS:
            variant = _GENERAL_ENGINE_VARIANTS[engine].get(cat)
            if variant and variant not in seen:
                result.append(variant)
                seen.add(variant)
            else:
                # No variant for this text category — use the general engine as-is
                result.append(engine)
                seen.add(engine)
        else:
            # Unknown engine — keep it and let SearXNG decide
            result.append(engine)
            seen.add(engine)

    return ",".join(result)


def _prefetch_tab_categories(query: str, pageno: str, current_cat: str,
                              config, selector) -> None:
    """Background-fetch the other visible tab categories so they're cache-warm
    before the user clicks them. Runs in a daemon thread; silently ignores errors."""
    tab_cats = config.prefs.get("tab_categories") or []
    # Skip "all" (too broad), skip current tab, keep only real category values
    to_fetch = [c for c in tab_cats if c != current_cat and c != "all" and c in _VALID_CATS]
    if not to_fetch:
        return

    def _run():
        for cat in to_fetch:
            try:
                key = _cache_key(query, pageno, cat, config.prefs)
                if _cache_get(key) is not None:
                    continue  # already cached — skip
                _do_search(query, config, selector, pageno, cat)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def _build_params(query: str, pageno: str, prefs: dict, categories: str | None, time_range: str = "") -> dict:
    p = {**prefs}
    if categories == "all":
        p.pop("categories", None)   # no restriction — SearXNG searches all categories
    elif categories:
        p["categories"] = categories
    # else: None → keep prefs["categories"] (defaults to "general")

    if categories and categories != "all":
        selected = _select_engines_for_category(p.get("engines", ""), categories)
        if selected:
            p["engines"] = selected
        else:
            p.pop("engines", None)

    params = {"q": query, "pageno": pageno, **{k: v for k, v in p.items() if k not in _NON_SEARXNG_PREFS}}
    # locale locks SearXNG's own UI language (affects how it queries backends).
    # Use language if set and valid, otherwise en-US.
    lang = params.get("language", "en-US")
    params.setdefault("locale", "en-US" if (not lang or lang == "all") else lang)

    # SearXNG engine names are always lowercase internally.  _select_engines_for_category
    # already lowercases when it runs (non-"all" categories), but for cat=all we skip
    # that path and the user's prefs may have capitalised names (e.g. "Google", "Brave").
    if "engines" in params:
        params["engines"] = ",".join(
            e.strip().lower() for e in params["engines"].split(",") if e.strip()
        )

    if time_range in ("day", "week", "month", "year"):
        params["time_range"] = time_range

    return params


def _do_search(query: str, config: Config, selector: InstanceSelector, pageno: str, categories: str | None = None, time_range: str = ""):
    langs = [l.strip() for l in config.prefs.get("language", "en-US").split(",") if l.strip()]
    multi = max(1, int(config.prefs.get("multi_instance", 1)))
    use_tor = _get_routing(config.prefs)
    custom_dns = config.prefs.get("custom_dns", "")

    use_cache = config.prefs.get("result_cache", True)
    if use_cache:
        key = _cache_key(query, pageno, categories, config.prefs, time_range)
        cached = _cache_get(key)
        if cached is not None:
            return cached, None

    if len(langs) > 1:
        results, err = _multi_language_search(langs, query, pageno, categories, config, selector, use_tor, custom_dns)
    else:
        params = _build_params(query, pageno, config.prefs, categories, time_range)
        if multi > 1:
            results, err = _multi_search(multi, params, config, selector, use_tor, custom_dns)
        else:
            results, err = _single_search(params, config, selector, use_tor, custom_dns, query, pageno)

    if results and use_cache:
        _cache_put(key, results, _query_ttl(query, categories or ""))
    return results, err


def _single_search(params, config, selector, use_tor, custom_dns, query, pageno):
    tried = set()
    limit = min(int(config.prefs.get("engine_failover_limit", 3)), max(len(config.instances), 1))
    _ft = int(config.prefs.get('failover_timeout', 12))

    for _ in range(limit):
        instance = selector.pick(exclude=tried)
        if not instance:
            break
        tried.add(instance)

        t0 = time.monotonic()
        result = fetch(instance + "/search", params, tor_port=config.tor_port,
                       timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                       tor_timeout=_ft)
        elapsed = time.monotonic() - t0

        if not result.ok:
            selector.mark_unhealthy(instance, cooldown=_failure_cooldown(result))
            continue

        selector.mark_healthy(instance)
        selector.record_time(instance, elapsed)
        parsed = parse(result.text, base_url=instance)

        if not parsed.results and not parsed.failed_engines:
            # 200 with empty body — try: Tor+bangs → direct → direct+bangs before giving up
            _req = {e.strip().lower() for e in params.get("engines", "").split(",") if e.strip()}
            _bangable = [e for e in _req if e in _ENGINE_BANGS] if _req else []
            if _bangable:
                _bp = {k: v for k, v in params.items() if k != "engines"}
                _bp["q"] = params["q"] + " " + " ".join(_ENGINE_BANGS[e] for e in _bangable)
                _br = fetch(instance + "/search", _bp, tor_port=config.tor_port,
                            timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                            tor_timeout=_ft)
                if _br.ok:
                    _bparsed = parse(_br.text, base_url=instance)
                    if _bparsed.results:
                        parsed = _bparsed
            if not parsed.results and use_tor == "tor_fallback":
                _dr = fetch(instance + "/search", params, timeout=config.request_timeout,
                            routing="direct", custom_dns=custom_dns)
                if _dr.ok:
                    _dp = parse(_dr.text, base_url=instance)
                    if _dp.results:
                        parsed = _dp
                if not parsed.results and _bangable:
                    _dbp = {k: v for k, v in params.items() if k != "engines"}
                    _dbp["q"] = params["q"] + " " + " ".join(_ENGINE_BANGS[e] for e in _bangable)
                    _dbr = fetch(instance + "/search", _dbp, timeout=config.request_timeout,
                                 routing="direct", custom_dns=custom_dns)
                    if _dbr.ok:
                        _dbparsed = parse(_dbr.text, base_url=instance)
                        if _dbparsed.results:
                            parsed = _dbparsed
            if not parsed.results:
                continue

        requested = {e.strip().lower() for e in params.get("engines", "").split(",") if e.strip()}
        found = {e.lower() for r in parsed.results for e in r.engines}
        explicitly_failed = {e.lower() for e in parsed.failed_engines}
        missing = list((requested - found) | explicitly_failed)

        if missing:
            supplement = _retry_engines(missing, query, pageno, config,
                                        selector, tried.copy(), use_tor, custom_dns)
            if supplement:
                parsed.results = merge_ranked([parsed.results, supplement],
                                              normalize=config.prefs.get("fuzzy_dedup", True))

        blocked = _blocked_engines_set(config.prefs)
        if blocked:
            parsed.results = [r for r in parsed.results
                              if not all(e.lower() in blocked for e in r.engines
                                         if e.lower() != "cached")]
        return parsed.results, None

    if not config.instances:
        return [], "No instances configured. Run <code>scrambler discover</code> and add some to your instance list via Settings."
    if config.prefs.get("direct_engine_fallback"):
        from .direct_engines import search_direct_engines as _sde
        _req = {e.strip().lower() for e in params.get("engines", "").split(",") if e.strip()}
        _dir = _sde(list(_req), query, pageno, config, use_tor)
        if _dir:
            return _dir, None
    return [], "All instances failed or are on cooldown. Try again shortly."


def _multi_language_search(langs: list, query: str, pageno: str, categories: str | None,
                            config: Config, selector: InstanceSelector, use_tor: str, custom_dns: str):
    # Pick one instance per language upfront (different instances where possible).
    tried: set = set()
    assignments = []
    for lang in langs:
        inst = selector.pick(exclude=tried) or selector.pick()
        if inst:
            tried.add(inst)
            assignments.append((lang, inst))

    if not assignments:
        if not config.instances:
            return [], "No instances configured. Add some via Settings."
        return [], "All instances failed or are on cooldown. Try again shortly."

    _lang_ft = int(config.prefs.get('failover_timeout', 12))

    def fetch_lang(lang: str, instance: str):
        lang_prefs = {**config.prefs, "language": lang}
        params = _build_params(query, pageno, lang_prefs, categories)
        t0 = time.monotonic()
        result = fetch(instance + "/search", params, tor_port=config.tor_port,
                       timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                       tor_timeout=_lang_ft)
        elapsed = time.monotonic() - t0
        if not result.ok:
            return instance, None, elapsed, result
        parsed = parse(result.text, base_url=instance)
        return instance, parsed.results if parsed.results else None, elapsed, result

    all_results = []
    with ThreadPoolExecutor(max_workers=len(assignments)) as executor:
        futures = {executor.submit(fetch_lang, lang, inst): inst for lang, inst in assignments}
        try:
            for future in as_completed(futures, timeout=_lang_ft + config.request_timeout + 5):
                inst, results, elapsed, fetch_result = future.result()
                if results is not None:
                    selector.mark_healthy(inst)
                    selector.record_time(inst, elapsed)
                    all_results.append(results)
                else:
                    selector.mark_unhealthy(inst, cooldown=_failure_cooldown(fetch_result))
        except concurrent.futures.TimeoutError:
            pass

    if not all_results:
        return [], "All language searches failed or timed out. Try again shortly."
    merged = merge_ranked(all_results, normalize=config.prefs.get("fuzzy_dedup", True))
    blocked = _blocked_engines_set(config.prefs)
    if blocked:
        merged = [r for r in merged
                  if not all(e.lower() in blocked for e in r.engines if e.lower() != "cached")]
    return merged, None


def _multi_search(multi: int, params: dict, config: Config, selector: InstanceSelector, use_tor: str, custom_dns: str):
    tried = set()
    instances = []
    for _ in range(multi):
        inst = selector.pick(exclude=tried)
        if inst:
            instances.append(inst)
            tried.add(inst)

    if not instances:
        if not config.instances:
            return [], "No instances configured. Run <code>scrambler discover</code> and add some to your instance list via Settings."
        return [], "All instances failed or are on cooldown. Try again shortly."

    _multi_ft = int(config.prefs.get('failover_timeout', 12))

    def fetch_one(instance: str):
        t0 = time.monotonic()
        result = fetch(instance + "/search", params, tor_port=config.tor_port,
                       timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                       tor_timeout=_multi_ft)
        elapsed = time.monotonic() - t0
        if not result.ok:
            return instance, None, elapsed, result
        parsed = parse(result.text, base_url=instance)
        results = list(parsed.results) if parsed.results else []
        requested_engines = {e.strip().lower() for e in params.get("engines", "").split(",") if e.strip()}
        # Bang via Tor: covers missing engines when results are partial, or all engines when empty
        if requested_engines:
            _found = {e.lower() for r in results for e in r.engines}
            _bangable = [e for e in (requested_engines - _found) if e in _ENGINE_BANGS]
            if _bangable:
                _bp = {k: v for k, v in params.items() if k != "engines"}
                _bp["q"] = params["q"] + " " + " ".join(_ENGINE_BANGS[e] for e in _bangable)
                _br = fetch(instance + "/search", _bp, tor_port=config.tor_port,
                            timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                            tor_timeout=_multi_ft)
                if _br.ok:
                    _bparsed = parse(_br.text, base_url=instance)
                    if _bparsed.results:
                        results = (merge_ranked([results, _bparsed.results],
                                               normalize=config.prefs.get("fuzzy_dedup", True))
                                   if results else list(_bparsed.results))
        # tor_fallback: if still no results, retry the same instance directly (Tor exit IP
        # may be blocked by underlying engines), then try direct + bangs
        if use_tor == "tor_fallback" and not results:
            _dr = fetch(instance + "/search", params, timeout=config.request_timeout,
                        routing="direct", custom_dns=custom_dns)
            if _dr.ok:
                _dp = parse(_dr.text, base_url=instance)
                results = list(_dp.results) if _dp.results else []
            if requested_engines and not results:
                _dfound = {e.lower() for r in results for e in r.engines}
                _dbangable = [e for e in (requested_engines - _dfound) if e in _ENGINE_BANGS]
                if _dbangable:
                    _dbp = {k: v for k, v in params.items() if k != "engines"}
                    _dbp["q"] = params["q"] + " " + " ".join(_ENGINE_BANGS[e] for e in _dbangable)
                    _dbr = fetch(instance + "/search", _dbp, timeout=config.request_timeout,
                                 routing="direct", custom_dns=custom_dns)
                    if _dbr.ok:
                        _dbparsed = parse(_dbr.text, base_url=instance)
                        if _dbparsed.results:
                            results = list(_dbparsed.results)
        return instance, results if results else None, elapsed, result

    all_results = []
    with ThreadPoolExecutor(max_workers=len(instances)) as executor:
        futures = {executor.submit(fetch_one, inst): inst for inst in instances}
        try:
            for future in as_completed(futures, timeout=_multi_ft + config.request_timeout + 5):
                inst, results, elapsed, fetch_result = future.result()
                if results is not None:
                    selector.mark_healthy(inst)
                    selector.record_time(inst, elapsed)
                    all_results.append(results)
                else:
                    selector.mark_unhealthy(inst, cooldown=_failure_cooldown(fetch_result))
        except concurrent.futures.TimeoutError:
            pass

    # All concurrent attempts failed — try additional instances sequentially before giving up
    if not all_results:
        fallback_limit = min(int(config.prefs.get("engine_failover_limit", 3)), len(config.instances))
        for _ in range(fallback_limit):
            inst = selector.pick(exclude=tried)
            if not inst:
                break
            tried.add(inst)
            t0 = time.monotonic()
            result = fetch(inst + "/search", params, tor_port=config.tor_port,
                           timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                           tor_timeout=_multi_ft)
            elapsed = time.monotonic() - t0
            if not result.ok:
                selector.mark_unhealthy(inst, cooldown=_failure_cooldown(result))
                continue
            parsed = parse(result.text, base_url=inst)
            if parsed.results:
                selector.mark_healthy(inst)
                selector.record_time(inst, elapsed)
                all_results.append(parsed.results)
                break
            # 200 but empty — skip without penalising

    if not all_results:
        if not config.instances:
            return [], "No instances configured. Run <code>scrambler discover</code> and add some to your instance list via Settings."
        if config.prefs.get("direct_engine_fallback"):
            from .direct_engines import search_direct_engines as _sde
            _req = {e.strip().lower() for e in params.get("engines", "").split(",") if e.strip()}
            _dir = _sde(list(_req), params["q"], params["pageno"], config, use_tor)
            if _dir:
                return _dir, None
        return [], "All instances failed or are on cooldown. Try again shortly."

    norm = config.prefs.get("fuzzy_dedup", True)
    merged = merge_ranked(all_results, normalize=norm)

    requested = {e.strip().lower() for e in params.get("engines", "").split(",") if e.strip()}
    if requested:
        flat = [r for batch in all_results for r in batch]
        found = {e.lower() for r in flat for e in r.engines}
        missing = list(requested - found)
        if missing:
            supplement = _retry_engines(missing, params["q"], params["pageno"], config,
                                        selector, tried, use_tor, custom_dns)
            if supplement:
                merged = merge_ranked([merged, supplement], normalize=norm)
        # Direct engine fallback for still-missing engines after _retry_engines
        if config.prefs.get("direct_engine_fallback"):
            from .direct_engines import search_direct_engines as _sde2
            flat2 = list(merged)
            found2 = {e.lower() for r in flat2 for e in r.engines}
            still_missing = [e for e in requested if e not in found2]
            if still_missing:
                _dir2 = _sde2(still_missing, params["q"], params["pageno"], config, use_tor)
                if _dir2:
                    merged = merge_ranked([merged, _dir2], normalize=norm)
    blocked = _blocked_engines_set(config.prefs)
    if blocked:
        merged = [r for r in merged
                  if not all(e.lower() in blocked for e in r.engines if e.lower() != "cached")]

    return merged, None


def _multi_search_streaming(multi: int, params: dict, config: Config, selector: InstanceSelector, use_tor: str, custom_dns: str, tried_out: set | None = None):
    """Generator yielding event dicts as instances respond.

    Event types:
      {"type": "init",    "instances": [url, ...]}
      {"type": "batch",   "instance": url, "results": [...], "engines": [...]}
      {"type": "fail",    "instance": url}
      {"type": "timeout", "instance": url}
    """
    tried: set = set()
    instances = []
    for _ in range(multi):
        inst = selector.pick(exclude=tried)
        if inst:
            instances.append(inst)
            tried.add(inst)
            if tried_out is not None:
                tried_out.add(inst)

    if not instances:
        return

    yield {"type": "init", "instances": instances}

    _ft = int(config.prefs.get("failover_timeout", 12))

    def fetch_one(instance: str):
        t0 = time.monotonic()
        result = fetch(instance + "/search", params, tor_port=config.tor_port,
                       timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                       tor_timeout=_ft)
        elapsed = time.monotonic() - t0
        if not result.ok:
            return instance, None, None, elapsed, result
        parsed = parse(result.text, base_url=instance)
        results = list(parsed.results) if parsed.results else []
        requested_engines = {e.strip().lower() for e in params.get("engines", "").split(",") if e.strip()}
        # Bang via Tor: covers missing engines when results are partial, or all engines when empty
        if requested_engines:
            _found = {e.lower() for r in results for e in r.engines}
            _bangable = [e for e in (requested_engines - _found) if e in _ENGINE_BANGS]
            if _bangable:
                _bp = {k: v for k, v in params.items() if k != "engines"}
                _bp["q"] = params["q"] + " " + " ".join(_ENGINE_BANGS[e] for e in _bangable)
                _br = fetch(instance + "/search", _bp, tor_port=config.tor_port,
                            timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                            tor_timeout=_ft)
                if _br.ok:
                    _bparsed = parse(_br.text, base_url=instance)
                    if _bparsed.results:
                        if results:
                            results = merge_ranked([results, _bparsed.results],
                                                  normalize=config.prefs.get("fuzzy_dedup", True))
                        else:
                            results = list(_bparsed.results)
                            parsed = _bparsed
        # tor_fallback: if still no results, retry the same instance directly (Tor exit IP
        # may be blocked by underlying engines), then try direct + bangs
        if use_tor == "tor_fallback" and not results:
            _dr = fetch(instance + "/search", params, timeout=config.request_timeout,
                        routing="direct", custom_dns=custom_dns)
            if _dr.ok:
                _dp = parse(_dr.text, base_url=instance)
                if _dp.results:
                    results = list(_dp.results)
                    parsed = _dp
            if requested_engines and not results:
                _dfound = {e.lower() for r in results for e in r.engines}
                _dbangable = [e for e in (requested_engines - _dfound) if e in _ENGINE_BANGS]
                if _dbangable:
                    _dbp = {k: v for k, v in params.items() if k != "engines"}
                    _dbp["q"] = params["q"] + " " + " ".join(_ENGINE_BANGS[e] for e in _dbangable)
                    _dbr = fetch(instance + "/search", _dbp, timeout=config.request_timeout,
                                 routing="direct", custom_dns=custom_dns)
                    if _dbr.ok:
                        _dbparsed = parse(_dbr.text, base_url=instance)
                        if _dbparsed.results:
                            results = list(_dbparsed.results)
                            parsed = _dbparsed
        return instance, results if results else None, parsed, elapsed, result

    any_success = False
    with ThreadPoolExecutor(max_workers=len(instances)) as executor:
        futures = {executor.submit(fetch_one, inst): inst for inst in instances}
        pending = set(instances)
        try:
            for future in as_completed(futures, timeout=_ft + config.request_timeout + 5):
                inst, results, parsed, elapsed, fetch_result = future.result()
                pending.discard(inst)
                if results is not None:
                    selector.mark_healthy(inst)
                    selector.record_time(inst, elapsed)
                    engines = sorted({e for r in results for e in r.engines})
                    yield {"type": "batch", "instance": inst, "results": results, "engines": engines, "elapsed": round(elapsed, 2)}
                    any_success = True
                elif parsed is not None and not getattr(parsed, "failed_engines", None):
                    pass  # 200 with empty body and no explicit failures — skip without penalising
                else:
                    selector.mark_unhealthy(inst, cooldown=_failure_cooldown(fetch_result))
                    yield {"type": "fail", "instance": inst, "elapsed": round(elapsed, 2)}
        except concurrent.futures.TimeoutError:
            for inst in pending:
                yield {"type": "timeout", "instance": inst}

    # Sequential fallback: if all concurrent attempts failed, try additional instances one at a time
    if not any_success:
        fallback_limit = min(int(config.prefs.get("engine_failover_limit", 3)), len(config.instances))
        for _ in range(fallback_limit):
            inst = selector.pick(exclude=tried)
            if not inst:
                break
            tried.add(inst)
            if tried_out is not None:
                tried_out.add(inst)
            yield {"type": "init_extra", "instance": inst}
            t0 = time.monotonic()
            result = fetch(inst + "/search", params, tor_port=config.tor_port,
                           timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                           tor_timeout=_ft)
            elapsed = time.monotonic() - t0
            if not result.ok:
                selector.mark_unhealthy(inst, cooldown=_failure_cooldown(result))
                yield {"type": "fail", "instance": inst}
                continue
            parsed = parse(result.text, base_url=inst)
            if parsed.results:
                selector.mark_healthy(inst)
                selector.record_time(inst, elapsed)
                engines = sorted({e for r in parsed.results for e in r.engines})
                yield {"type": "batch", "instance": inst, "results": parsed.results, "engines": engines, "elapsed": round(elapsed, 2)}
                break
            yield {"type": "fail", "instance": inst, "elapsed": round(elapsed, 2)}


def _retry_engines_streaming(missing_engines: list, query: str, pageno: str, config: Config, selector: InstanceSelector, already_tried: set, use_tor: str, custom_dns: str):
    """Generator version of _retry_engines — yields batch event dicts as each failover instance responds."""
    raw_limit = int(config.prefs.get("engine_failover_limit", 3))
    if raw_limit <= 0 or not missing_engines:
        return
    max_attempts = min(raw_limit, max(len(config.instances), 1))
    remaining = list(missing_engines)
    tried = set(already_tried)
    _ft = int(config.prefs.get("failover_timeout", 12))
    _phase_budget = int(config.prefs.get("failover_phase_timeout", 60))
    _failover_start = time.monotonic()

    for _ in range(max_attempts):
        # Total time cap on the failover phase so the finalize block (and final AI rerank)
        # runs within a predictable window regardless of engine_failover_limit.
        if _phase_budget > 0 and time.monotonic() - _failover_start > _phase_budget:
            break
        if not remaining:
            break
        instance = selector.pick_with_engines(remaining, exclude=tried)
        if not instance:
            break
        tried.add(instance)

        retry_params = _build_params(query, pageno, {**config.prefs, "engines": ",".join(remaining)}, None)
        result = fetch(instance + "/search", retry_params, tor_port=config.tor_port,
                       timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                       tor_timeout=_ft)
        if not result.ok:
            selector.mark_unhealthy(instance, cooldown=_failure_cooldown(result))
            continue

        selector.mark_healthy(instance)
        parsed = parse(result.text, base_url=instance)
        if parsed.results:
            engines = sorted({e for r in parsed.results for e in r.engines})
            yield {"type": "batch", "instance": instance, "results": parsed.results, "engines": engines}
            found = {e.lower() for r in parsed.results for e in r.engines}
            remaining = [e for e in remaining if e.lower() not in found]
        else:
            explicitly_failed = {e.lower() for e in getattr(parsed, "failed_engines", [])}
            remaining = [e for e in remaining if e.lower() not in explicitly_failed]


def _retry_engines(missing_engines: list, query: str, pageno: str, config: Config, selector: InstanceSelector, already_tried: set, use_tor: str, custom_dns: str) -> list:
    """Query fresh instances for engines that returned no results, one instance at a time,
    dropping engines from the retry list as they start producing results."""
    if not missing_engines:
        return []

    raw_limit = int(config.prefs.get("engine_failover_limit", 3))
    if raw_limit <= 0:
        return []
    max_attempts = min(raw_limit, max(len(config.instances), 1))

    all_results = []
    remaining = list(missing_engines)
    tried = set(already_tried)

    for _ in range(max_attempts):
        if not remaining:
            break
        instance = selector.pick_with_engines(remaining, exclude=tried)
        if not instance:
            break
        tried.add(instance)

        params = _build_params(query, pageno, {**config.prefs, "engines": ",".join(remaining)}, None)
        _retry_ft = int(config.prefs.get('failover_timeout', 12))
        result = fetch(instance + "/search", params, tor_port=config.tor_port,
                       timeout=config.request_timeout, routing=use_tor, custom_dns=custom_dns,
                       tor_timeout=_retry_ft)
        if not result.ok:
            selector.mark_unhealthy(instance, cooldown=_failure_cooldown(result))
            continue

        selector.mark_healthy(instance)
        parsed = parse(result.text, base_url=instance)
        if parsed.results:
            all_results.extend(parsed.results)
            found = {e.lower() for r in parsed.results for e in r.engines}
            remaining = [e for e in remaining if e not in found]

    return all_results
