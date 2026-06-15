import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_COLORS: dict = {
    "--bg":           "#111111",
    "--surface":      "#1c1c1c",
    "--border":       "#2a2a2a",
    "--text":         "#d8d8d8",
    "--muted":        "#666666",
    "--title":        "#5b9cf6",
    "--accent":       "#5b9cf6",
    "--accent-dim":   "#3a6ab5",
    "--visited":      "#9b72c8",
    "--url":          "#4caf7d",
    "--error":        "#e05a5a",
    "--engine":       "#333333",
    "--engine-text":  "#999999",
}

DEFAULT_APPEARANCE: dict = {
    "title":            "Scrambler",
    "subtitle":         "queries never logged — tor-routed — no single party holds your identity and your query",
    "tagline":          "Your queries, scattered across many instances through Tor.\nNo single party holds enough to read the story.",
    "favicon":          "\U0001f50d",
    "font_family":      "Inter",
    "font_import_url":  "",
    "font_face_css":    "",
    "background_image":   "",
    "background_size":    "cover",
    "background_opacity": 0.15,
    "background_video_loop": True,
    "loading_bg_url":        "",
    "loading_bg_opacity":    1.0,
    "loading_bg_loop":       True,
    "loading_bg_in_live":    False,
    "loading_quips":    [],
    "loading_bar_style": "crawl",
    "loading_indicator": "bar",    # non-live: "bar"|"console"|"none" / live: "tree"|"console"|"none"
    "christmas_tree_style": "dots",
    "background_audio_url":    "",
    "background_audio_volume": 0.5,
    "background_audio_loop":   True,
    "cursor_url":       "",
    "cursor_trail":     "off",
    "cursor_trail_url": "",
    "typing_sound":     "off",
    "typing_sound_url": "",
    "search_sound":     "off",
    "search_sound_url": "",
    "click_sound":      "off",
    "click_sound_url":  "",
    "sound_volume":     0.5,
    "colors":           DEFAULT_COLORS,
}

DEFAULT_PREFS: dict = {
    "language": "en-US",
    "safesearch": "0",
    "categories": "general",
    "engines": "duckduckgo,startpage,brave",
    "theme": "simple",
    "multi_instance": 1,
    "use_tor": "tor",
    "dearrow": False,
    "custom_dns": "",
    "weighted_selection": True,
    "result_cache": True,
    "circuit_warmup": 0,
    "priority_sources": [],
    "demote_domains": [],
    "demote_except_categories": ["social media"],
    "engine_ranking": False,
    "consensus_ranking": False,
    "lexical_scoring": False,
    "semantic_reranking": False,
    "semantic_model": "all-MiniLM-L6-v2",
    "semantic_cutoff": 0.0,
    "priority_source_cap": 1,
    "priority_source_cutoff": 0.0,
    "server_port": 7777,
    "streaming_results": "off",     # "off" | "live"
    "active_profile": "",
    "settings_customized": False,
    "default_category": "all",       # tab active when a fresh search is submitted
    "engine_failover_limit": 3,      # max extra instance attempts to fill missing engines
    "tab_categories": ["all", "general", "news", "images", "videos", "map", "music", "science", "it", "files", "social media"],
    "wayback_links": True,            # show archive.org link on each result
    "reader_mode": "both",           # "off" | "both" (direct + read badge) | "reader" (title → Tor)
    "thumbnail_proxy": "direct",     # "direct" | "tor" | "tor_fallback"
    "map_tile_provider": "openstreetmap",  # "openstreetmap" | "carto_light" | "carto_dark" | "custom" | "none"
    "map_tile_custom_url": "",             # tile URL template used when map_tile_provider == "custom"
    "route_provider": "osrm_public",       # "osrm_public" | "custom"
    "route_custom_url": "",               # OSRM-compatible base URL used when route_provider == "custom"
    "route_transit_url": "https://api.transitous.org/otp",  # OTP-compatible base URL for transit routing
    "transit_navitia_key": "",            # Navitia.io API key (free at navitia.io) — overrides OTP when set
    "map_geolocation": "hybrid",           # "off" | "low" | "high" | "hybrid" (low then high)
    "map_offline_method": "none",          # "none" | "service_worker" | "pmtiles" | "both"
    "map_offline_behavior": "hybrid",      # "hybrid" (live APIs) | "offline" (tiles only, no API calls)
    "map_pmtiles_url": "",                 # URL to .pmtiles archive (used when method is pmtiles or both)
    "map_tile_routing": "tor",             # "tor" | "tor_fallback" | "direct" — how tiles are fetched server-side
    "map_units": "metric",                 # "metric" | "imperial"
    "map_coord_format": "decimal",         # "decimal" | "dms" | "mgrs"
    "map_routing": "tor",                  # "tor" | "tor_fallback" | "direct" — geocoding + directions
    "failover_timeout": 12,                # seconds to wait on Tor before falling back (tor_fallback mode)
    "ai_provider": "",              # "anthropic" | "openai_compat" | "ollama" | ""
    "ai_api_key": "",               # stored server-side only, never sent to browser
    "ai_base_url": "",              # base URL for openai_compat or ollama
    "ai_model": "",                 # model name (e.g. claude-sonnet-4-6, gpt-4o, llama3)
    "ai_max_results": 10,           # number of results to include in the prompt
    "ai_routing": "direct",         # "direct" | "tor" | "tor_fallback"
    "ai_system_prompt": "",         # empty = use built-in default
    "ai_avatar": "",                # emoji or URL; empty = use Scrambler favicon
    "ai_user_avatar": "",           # emoji or URL; empty = default 👤
    "ai_reranking": False,          # send top N results to AI for final reranking
    "ai_rerank_count": 20,          # how many results to send to AI for reranking (1-100)
    "ai_rerank_timing": "final",    # "final" (end only) | "streaming" (every batch rerank)
    "ai_rerank_use_search_ai": True, # use the same AI as the summary, or a dedicated one
    "ai_rerank_provider": "",        # provider for dedicated reranker (same values as ai_provider)
    "ai_rerank_api_key": "",         # API key for dedicated reranker
    "ai_rerank_model": "",           # model for dedicated reranker
    "ai_rerank_base_url": "",        # base URL for dedicated reranker
    "ai_rerank_routing": "direct",   # routing for dedicated reranker
    "domain_cap": 3,                 # max results per domain before overflow moves to end (0=off)
    "freshness_boost": False,        # blend publication date into ranking for temporal queries
    "fuzzy_dedup": True,             # normalize URLs before dedup (strips www, tracking params)
    "autopick_schedule": "never",   # "never" | "boot" | "interval" | "search"
    "autopick_interval": 60,         # minutes (used when schedule == "interval")
    "autopick_count": 5,             # 1–100
    "autopick_min_engines": 5,       # minimum working engines an instance must have
    "appearance": DEFAULT_APPEARANCE,
}


@dataclass
class Config:
    instances: list
    prefs: dict
    tor_port: int = 9050
    server_port: int = 7777
    request_timeout: int = 15
    failover_limit: int = 5
    unhealthy_cooldown: int = 300


def load_config(config_dir: Path) -> Config:
    return Config(
        instances=_load_instances(config_dir / "instances.txt"),
        prefs=_load_prefs(config_dir / "preferences.json"),
    )


def _load_instances(path: Path) -> list:
    if not path.exists():
        return []
    urls = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            urls.append(line.rstrip("/"))
    return urls


def _load_prefs(path: Path) -> dict:
    if not path.exists():
        return dict(DEFAULT_PREFS)
    try:
        data = json.loads(path.read_text())
        prefs = {**DEFAULT_PREFS, **data}
        # Migrate old boolean use_tor to the 3-way string format
        if isinstance(prefs.get("use_tor"), bool):
            prefs["use_tor"] = "tor" if prefs["use_tor"] else "direct"
        # Deep-merge appearance so new defaults aren't lost when only some keys were saved
        saved_app = data.get("appearance") or {}
        prefs["appearance"] = {
            **DEFAULT_APPEARANCE,
            **saved_app,
            "colors": {**DEFAULT_COLORS, **(saved_app.get("colors") or {})},
        }
        return prefs
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_PREFS)


def save_instances(path: Path, instances: list) -> None:
    path.write_text("\n".join(instances) + "\n" if instances else "")


def save_prefs(path: Path, prefs: dict) -> None:
    path.write_text(json.dumps(prefs, indent=2) + "\n")


def _make_theme(colors: dict, **overrides) -> dict:
    return {**DEFAULT_APPEARANCE, **overrides, "colors": {**DEFAULT_COLORS, **colors}}


DEFAULT_THEMES: dict = {
    "Dark": _make_theme({
        "--bg": "#111111", "--surface": "#1c1c1c", "--border": "#2a2a2a",
        "--text": "#d8d8d8", "--muted": "#666666", "--title": "#5b9cf6",
        "--accent": "#5b9cf6", "--accent-dim": "#3a6ab5", "--visited": "#9b72c8",
        "--url": "#4caf7d", "--error": "#e05a5a", "--engine": "#333333", "--engine-text": "#999999",
    }),
    "Light": _make_theme({
        "--bg": "#f5f5f5", "--surface": "#ffffff", "--border": "#e0e0e0",
        "--text": "#1a1a1a", "--muted": "#888888", "--title": "#2563eb",
        "--accent": "#2563eb", "--accent-dim": "#1d4ed8", "--visited": "#7c3aed",
        "--url": "#16a34a", "--error": "#dc2626", "--engine": "#eeeeee", "--engine-text": "#555555",
    }),
    "Nord": _make_theme({
        "--bg": "#2e3440", "--surface": "#3b4252", "--border": "#434c5e",
        "--text": "#eceff4", "--muted": "#4c566a", "--title": "#88c0d0",
        "--accent": "#88c0d0", "--accent-dim": "#5e81ac", "--visited": "#b48ead",
        "--url": "#a3be8c", "--error": "#bf616a", "--engine": "#434c5e", "--engine-text": "#d8dee9",
    }),
    "Catppuccin": _make_theme({
        "--bg": "#1e1e2e", "--surface": "#313244", "--border": "#45475a",
        "--text": "#cdd6f4", "--muted": "#6c7086", "--title": "#cba6f7",
        "--accent": "#89b4fa", "--accent-dim": "#74c7ec", "--visited": "#cba6f7",
        "--url": "#a6e3a1", "--error": "#f38ba8", "--engine": "#45475a", "--engine-text": "#a6adc8",
    }),
    "Solarized": _make_theme({
        "--bg": "#002b36", "--surface": "#073642", "--border": "#094555",
        "--text": "#839496", "--muted": "#586e75", "--title": "#268bd2",
        "--accent": "#268bd2", "--accent-dim": "#2aa198", "--visited": "#6c71c4",
        "--url": "#859900", "--error": "#dc322f", "--engine": "#073642", "--engine-text": "#657b83",
    }),
    "Breakfast": _make_theme({
        "--bg":           "#1a1208",
        "--surface":      "#2c1f0d",
        "--border":       "#4a3318",
        "--text":         "#f5e6c8",
        "--muted":        "#9a7a4a",
        "--title":        "#f5c518",
        "--accent":       "#f5c518",
        "--accent-dim":   "#c9a012",
        "--visited":      "#e8913a",
        "--url":          "#8bbf5a",
        "--error":        "#e05a5a",
        "--engine":       "#2c1f0d",
        "--engine-text":  "#9a7a4a",
    },
        title="Scrambler",
        subtitle="freshly scrambled results — served hot — no stale queries — no tracking",
        tagline="Your search, cracked open and scrambled fresh.\nRouted through Tor so nobody knows what you're hungry for.",
        favicon="🍳",
        font_family="Righteous",
        background_image="/static/backgrounds/breakfast.gif",
        background_opacity=0.18,
        loading_quips=[
            "Cracking the eggs...",
            "Heating up the griddle...",
            "Scrambling your privacy...",
            "Toasting your anonymity...",
            "Brewing anonymous connections...",
            "Buttering up the instances...",
            "Whisking the Tor circuits...",
            "Almost on the plate...",
        ],
    ),
    "Hacker": _make_theme({
        "--bg": "#030d03", "--surface": "#071407", "--border": "#0d3a0d",
        "--text": "#33ff33", "--muted": "#1a7a1a", "--title": "#39ff14",
        "--accent": "#39ff14", "--accent-dim": "#1da81d", "--visited": "#00cc44",
        "--url": "#00ff88", "--error": "#ff3333", "--engine": "#071407", "--engine-text": "#1a7a1a",
    },
        title="> $CR4M8L3R_",
        subtitle="[T0R R0UT1NG 4CT1V3] [QU3R13$ 3NCRYPT3D] [N0 L0G$ K3PT]",
        tagline="> 1N1T14L1Z1NG 4N0NYM0U$ QU3RY PR0T0C0L...\n> R0UT1NG THR0UGH T0R N3TW0RK...\n> 1D3NT1TY C0NC34L3D. $T4ND1NG BY_",
        favicon=">",
        font_family="VT323",
        loading_indicator="console",
        loading_indicator_live="console",
        loading_indicator_static="console",
        background_audio_loop="0",
        loading_quips=[
            "> 1N1T14L1Z1NG T0R PR0T0C0L...",
            "> C0NN3CT1NG T0 3X1T N0D3S...",
            "> 3NCRYPT1NG 1D3NT1TY M4TR1X...",
            "> $3L3CT1NG 4N0NYM0U$ 1N$T4NC3S...",
            "> R0UT1NG THR0UGH 0N10N L4Y3R$...",
            "> 3V4D1NG $URV31LL4NC3...",
            "> 4N0NYM1TY C0NF1RM3D...",
            "> $T4ND1NG BY_",
        ],
    ),
}


def seed_themes(path: Path) -> None:
    themes = load_themes(path)
    themes.update(DEFAULT_THEMES)  # built-in themes always reflect the latest code
    save_themes(path, themes)


PROFILE_PREFS: frozenset = frozenset({
    # Search behaviour
    "streaming_results",
    "language", "safesearch", "categories", "engines",
    "priority_sources", "priority_source_cap", "priority_source_cutoff",
    "demote_domains", "demote_except_categories",
    "engine_ranking", "consensus_ranking",
    "lexical_scoring", "semantic_reranking", "semantic_model", "semantic_cutoff",
    "multi_instance", "weighted_selection", "result_cache",
    "use_tor", "circuit_warmup",
    "default_category", "dearrow", "thumbnail_proxy", "reader_mode", "wayback_links",
    "tab_categories", "domain_cap", "freshness_boost",
    "fuzzy_dedup", "engine_failover_limit", "failover_phase_timeout",
    "direct_engine_fallback", "blocked_engines",
    # Auto-pick
    "autopick_schedule", "autopick_interval", "autopick_count", "autopick_min_engines",
    # Map
    "map_tile_provider", "map_tile_custom_url",
    "route_provider", "route_custom_url", "route_transit_url",
    "map_geolocation", "map_offline_method", "map_offline_behavior", "map_pmtiles_url", "map_units", "map_coord_format", "map_routing", "map_tile_routing",
    "failover_timeout",
    # AI (api keys never stored in profiles)
    "ai_provider", "ai_base_url", "ai_model", "ai_max_results",
    "ai_routing", "ai_system_prompt", "ai_avatar", "ai_user_avatar",
    "ai_use_jina", "jina_routing", "jina_reader",
    "ai_reranking", "ai_rerank_count", "ai_rerank_timing", "ai_rerank_on_ask",
    "ai_rerank_use_search_ai", "ai_rerank_provider", "ai_rerank_model",
    "ai_rerank_base_url", "ai_rerank_routing",
    "img_gen_provider", "img_gen_base_url", "img_gen_model", "img_gen_size",
    "img_gen_steps", "img_gen_cfg_scale", "img_gen_negative_prompt",
})

# Fields that contain secrets — stripped from all exports and never saved in profiles
_EXPORT_SENSITIVE: frozenset = frozenset({
    "ai_api_key", "jina_api_key", "transit_navitia_key",
    "ai_rerank_api_key", "img_gen_api_key",
})


def _profile(engines, categories="general", safesearch="0", language="en-US",
             engine_ranking=False, consensus_ranking=False,
             lexical_scoring=False, semantic_reranking=False,
             semantic_model="all-MiniLM-L6-v2", semantic_cutoff=0.0,
             priority_sources=None, priority_source_cap=1, priority_source_cutoff=0.0,
             multi_instance=1, weighted_selection=True, result_cache=True,
             use_tor=None, circuit_warmup=None) -> dict:
    d = {
        "engines": engines,
        "categories": categories,
        "safesearch": safesearch,
        "language": language,
        "engine_ranking": engine_ranking,
        "consensus_ranking": consensus_ranking,
        "lexical_scoring": lexical_scoring,
        "semantic_reranking": semantic_reranking,
        "semantic_model": semantic_model,
        "semantic_cutoff": semantic_cutoff,
        "priority_sources": priority_sources or [],
        "priority_source_cap": priority_source_cap,
        "priority_source_cutoff": priority_source_cutoff,
        "multi_instance": multi_instance,
        "weighted_selection": weighted_selection,
        "result_cache": result_cache,
    }
    # Only include routing overrides when explicitly set — lets profiles stay
    # routing-agnostic by default so loading one doesn't flip the system Tor switch.
    if use_tor is not None:
        d["use_tor"] = use_tor
    if circuit_warmup is not None:
        d["circuit_warmup"] = circuit_warmup
    return d


DEFAULT_PROFILES: dict = {
    # ── Daily driver — left intentionally minimal ─────────────────────────────
    "Default": _profile(
        engines="duckduckgo,startpage,brave,bing,qwant",
        engine_ranking=True,
        consensus_ranking=True,
        lexical_scoring=True,
        multi_instance=2,
        weighted_selection=True,
        result_cache=True,
    ),

    # ── General ──────────────────────────────────────────────────────────────
    "Deep Dive": {
        **_profile(
            engines="duckduckgo,startpage,brave,bing,mojeek,qwant",
            engine_ranking=True,
            consensus_ranking=True,
            lexical_scoring=True,
            semantic_reranking=True,
            semantic_model="all-mpnet-base-v2",
            semantic_cutoff=0.10,
            multi_instance=3,
            weighted_selection=True,
            result_cache=True,
        ),
        "streaming_results": "live",
        "default_category": "all",
        "ai_max_results": 15,
        "jina_reader": True,
        "jina_routing": "direct",
        "autopick_schedule": "boot",
        "autopick_count": 5,
        "autopick_min_engines": 5,
    },

    # ── Topic-specific ───────────────────────────────────────────────────────
    "Research": {
        **_profile(
            engines="google scholar,semantic scholar,pubmed,arxiv,wolframalpha,base,startpage,brave,duckduckgo,bing,qwant",
            engine_ranking=True,
            consensus_ranking=True,
            lexical_scoring=True,
            semantic_reranking=True,
            semantic_model="all-mpnet-base-v2",
            semantic_cutoff=0.15,
            priority_sources=[
                "arxiv.org",
                "wikipedia.org",
                "pubmed.ncbi.nlm.nih.gov",
                "ncbi.nlm.nih.gov",
                "semanticscholar.org",
                "scholar.google.com",
                "jstor.org",
                "nature.com",
                "sciencedirect.com",
                "springer.com",
                "plos.org",
            ],
            priority_source_cap=3,
            priority_source_cutoff=0.15,
            multi_instance=3,
            weighted_selection=True,
            result_cache=True,
        ),
        "streaming_results": "live",
        "default_category": "all",
        "ai_max_results": 15,
        "ai_use_jina": True,
        "jina_reader": True,
        "jina_routing": "direct",
        "autopick_schedule": "boot",
        "autopick_count": 5,
        "autopick_min_engines": 6,
    },
    "Science": {
        **_profile(
            engines="startpage,brave,duckduckgo,bing",
            categories="science",
            engine_ranking=True,
            consensus_ranking=True,
            lexical_scoring=True,
            semantic_reranking=True,
            semantic_model="all-MiniLM-L6-v2",
            semantic_cutoff=0.15,
            priority_sources=[
                "arxiv.org",
                "nature.com",
                "science.org",
                "pubmed.ncbi.nlm.nih.gov",
                "nih.gov",
                "plos.org",
                "sciencedirect.com",
                "newscientist.com",
                "scientificamerican.com",
            ],
            priority_source_cap=2,
            priority_source_cutoff=0.15,
            multi_instance=2,
            weighted_selection=True,
            result_cache=True,
        ),
        "streaming_results": "live",
        "default_category": "science",
        "ai_max_results": 10,
        "ai_use_jina": True,
        "jina_reader": True,
        "jina_routing": "direct",
    },
    "Tech": {
        **_profile(
            engines="startpage,brave,duckduckgo,bing",
            categories="it",
            engine_ranking=True,
            consensus_ranking=True,
            lexical_scoring=True,
            semantic_reranking=False,
            priority_sources=[
                "github.com",
                "stackoverflow.com",
                "developer.mozilla.org",
                "docs.python.org",
                "pkg.go.dev",
                "docs.rs",
                "rust-lang.org",
                "crates.io",
                "npmjs.com",
                "pypi.org",
                "learn.microsoft.com",
                "developer.apple.com",
                "man7.org",
                "wiki.archlinux.org",
            ],
            priority_source_cap=2,
            priority_source_cutoff=0.20,
            multi_instance=2,
            weighted_selection=True,
            result_cache=True,
        ),
        "streaming_results": "live",
        "default_category": "it",
        "ai_max_results": 10,
        "jina_reader": True,
        "jina_routing": "direct",
        "dearrow": False,
    },
    "News": {
        **_profile(
            engines="duckduckgo,brave,bing,qwant",
            categories="news",
            engine_ranking=False,
            consensus_ranking=True,
            lexical_scoring=True,
            semantic_reranking=False,
            priority_sources=[
                "reuters.com",
                "apnews.com",
                "bbc.com",
                "theguardian.com",
                "npr.org",
            ],
            priority_source_cap=1,
            priority_source_cutoff=0.10,
            multi_instance=2,
            weighted_selection=True,
            result_cache=False,
        ),
        "streaming_results": "live",
        "default_category": "news",
        "dearrow": False,
        "jina_reader": True,
        "jina_routing": "direct",
        "autopick_schedule": "search",
        "autopick_count": 4,
        "autopick_min_engines": 3,
        "ai_max_results": 10,
    },

    # ── Media ────────────────────────────────────────────────────────────────
    "Videos": {
        **_profile(
            engines="youtube,invidious,dailymotion,vimeo,odysee",
            categories="videos",
            engine_ranking=False,
            consensus_ranking=True,
            lexical_scoring=False,
            semantic_reranking=False,
            multi_instance=2,
            weighted_selection=True,
            result_cache=True,
        ),
        "streaming_results": "live",
        "default_category": "videos",
        "dearrow": True,
        "ai_max_results": 5,
    },
    "Images": {
        **_profile(
            engines="bing images,brave.images,duckduckgo images,google images",
            categories="images",
            engine_ranking=False,
            consensus_ranking=True,
            lexical_scoring=False,
            semantic_reranking=False,
            multi_instance=2,
            weighted_selection=True,
            result_cache=True,
        ),
        "streaming_results": "live",
        "default_category": "images",
    },

    # ── Maps ─────────────────────────────────────────────────────────────────
    "Maps": {
        **_profile(
            engines="openstreetmap",
            categories="map",
            engine_ranking=False,
            consensus_ranking=False,
            lexical_scoring=False,
            semantic_reranking=False,
            multi_instance=1,
            weighted_selection=False,
            result_cache=False,
        ),
        "streaming_results": "live",
        "default_category": "map",
        "tab_categories": ["map", "all", "general"],
        "domain_cap": 0,            # show every map result; no cap needed
        "freshness_boost": False,
        "fuzzy_dedup": False,       # map URLs differ by coords — keep them distinct
        "engine_failover_limit": 0, # map queries are one-shot; no failover needed
        "demote_domains": [],
        "demote_except_categories": [],
        "reader_mode": "off",
        "thumbnail_proxy": "direct",
        "dearrow": False,
        "map_tile_provider": "openstreetmap",
        "map_geolocation": "hybrid",
        "map_offline_method": "none",
        "map_offline_behavior": "hybrid",
    },

    # ── Speed / Privacy extremes ─────────────────────────────────────────────
    "Performance": {
        **_profile(
            engines="duckduckgo,brave,bing",
            engine_ranking=False,
            consensus_ranking=False,
            lexical_scoring=False,
            semantic_reranking=False,
            multi_instance=1,
            weighted_selection=True,
            result_cache=True,
            use_tor="direct",
            circuit_warmup=0,
        ),
        "streaming_results": "live",
        "default_category": "all",
        "autopick_schedule": "boot",
        "autopick_count": 3,
        "autopick_min_engines": 3,
        "freshness_boost": False,   # skip temporal analysis pass
        "fuzzy_dedup": True,
        "domain_cap": 3,
        "engine_failover_limit": 0, # accept first-round results; no retry overhead
        "demote_domains": [],
        "demote_except_categories": ["social media"],
        "thumbnail_proxy": "direct",
        "reader_mode": "off",
        "dearrow": False,
        "map_tile_provider": "openstreetmap",
        "map_geolocation": "low",   # fast WiFi/IP fix, no GPS wait
        "map_offline_method": "none",
        "map_offline_behavior": "hybrid",
        "ai_routing": "direct",
        "ai_use_jina": False,
        "jina_reader": False,
        "jina_routing": "direct",
    },
    "Privacy": {
        **_profile(
            engines="mojeek,brave,duckduckgo,qwant,startpage",
            engine_ranking=True,
            consensus_ranking=True,
            lexical_scoring=False,
            semantic_reranking=False,
            priority_sources=[
                "privacyguides.org",
                "eff.org",
                "ssd.eff.org",
                "spreadprivacy.com",
            ],
            priority_source_cap=1,
            priority_source_cutoff=0.0,
            multi_instance=2,
            weighted_selection=True,
            result_cache=True,
            use_tor="tor",
            circuit_warmup=0,  # prewarming creates persistent circuits that aid correlation
        ),
        "streaming_results": "live",
        "default_category": "all",
        "autopick_schedule": "boot",
        "autopick_count": 5,
        "autopick_min_engines": 4,
        "freshness_boost": False,   # temporal queries expose interests; skip
        "fuzzy_dedup": True,        # strip tracking params before dedup
        "domain_cap": 3,
        "engine_failover_limit": 0, # every extra query is another data point leaked
        "demote_domains": [],
        "demote_except_categories": ["social media"],
        "thumbnail_proxy": "tor",
        "reader_mode": "reader",
        "dearrow": False,           # DeArrow is a third-party external call
        "map_tile_provider": "none",
        "map_offline_method": "pmtiles",
        "map_offline_behavior": "offline",  # no live API calls; tiles only
        "map_geolocation": "off",
        "ai_provider": "ollama",
        "ai_routing": "direct",
        "jina_reader": True,
        "jina_routing": "tor",
    },
}


def load_search_profiles(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_search_profiles(path: Path, profiles: dict) -> None:
    path.write_text(json.dumps(profiles, indent=2) + "\n")


def seed_search_profiles(path: Path) -> None:
    """Always sync built-in profiles to current defaults. User-created profiles are preserved."""
    profiles = load_search_profiles(path)
    profiles.update(DEFAULT_PROFILES)
    save_search_profiles(path, profiles)


def load_instance_engines(path: Path) -> dict:
    """Return {url: [engine_name, ...]} mapping."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_instance_engines(path: Path, engine_map: dict) -> None:
    path.write_text(json.dumps(engine_map, indent=2) + "\n")


def load_themes(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_themes(path: Path, themes: dict) -> None:
    path.write_text(json.dumps(themes, indent=2) + "\n")


AI_PERSONALITY_KEYS: frozenset = frozenset({
    "ai_provider", "ai_base_url", "ai_model", "ai_max_results",
    "ai_routing", "ai_system_prompt", "ai_avatar", "ai_user_avatar",
    "ai_use_jina", "jina_routing",
})


def load_ai_personalities(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_ai_personalities(path: Path, personalities: dict) -> None:
    path.write_text(json.dumps(personalities, indent=2) + "\n")


def load_map_data(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_map_data(path: Path, data: list) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")
