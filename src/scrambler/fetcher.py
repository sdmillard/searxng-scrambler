import socket
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from . import circuits

# Thread-local slot for a custom DNS resolver.
# When set, _patched_getaddrinfo uses it instead of the OS resolver.
_local = threading.local()
_real_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    resolver = getattr(_local, "resolver", None)
    if resolver is not None:
        try:
            import dns.resolver
            answers = resolver.resolve(host, "A")
            return _real_getaddrinfo(str(answers[0]), port, family, type, proto, flags)
        except Exception:
            pass
    return _real_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _patched_getaddrinfo


@dataclass
class FetchResult:
    ok: bool
    status_code: int | None
    text: str | None
    error: str | None
    captcha: bool = False


_CAPTCHA_SIGNALS = (
    "captcha", "recaptcha", "hcaptcha", "verify you are human",
    "i'm not a robot", "are you a human",
)


def _accept_language(lang: str) -> str:
    # SearXNG uses "all" as a special value — not a valid BCP-47 tag.
    # Fall back to en-US so the header is always valid.
    if not lang or lang == "all":
        return "en-US,en;q=0.9"
    base = lang.split("-")[0].split("_")[0]
    parts = [lang]
    if base.lower() != lang.lower():
        parts.append(f"{base};q=0.9")
    if not base.lower().startswith("en"):
        parts.append("en;q=0.5")
    return ",".join(parts)


def check_tor(port: int = 9050) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _do_request(
    url: str,
    params: dict,
    proxies: dict,
    timeout: int,
    is_direct: bool,
    custom_dns: str,
) -> FetchResult:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": _accept_language(params.get("language", "en-US")),
        "Accept-Encoding": "gzip, deflate, br",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"
        ),
        "Referer": origin + "/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }

    if is_direct and custom_dns:
        import dns.resolver as _dns_module
        r = _dns_module.Resolver()
        r.nameservers = [custom_dns]
        _local.resolver = r
    else:
        _local.resolver = None

    # SearXNG parses engines via getlist(), so send repeated params not a comma-joined string.
    # Convert {"engines": "bing,google"} → [("engines","bing"),("engines","google")] so
    # requests produces ?engines=bing&engines=google instead of ?engines=bing%2Cgoogle.
    req_params: list | dict = params
    engines_val = params.get("engines", "")
    if isinstance(engines_val, str) and engines_val:
        engines_list = [e.strip() for e in engines_val.split(",") if e.strip()]
        if len(engines_list) > 1:
            req_params = [(k, v) for k, v in params.items() if k != "engines"]
            req_params += [("engines", e) for e in engines_list]

    try:
        resp = requests.get(
            url,
            params=req_params,
            proxies=proxies,
            timeout=timeout,
            headers=headers,
            allow_redirects=True,
        )
        text = resp.text
        captcha = _is_captcha(resp.status_code, text)
        return FetchResult(
            ok=resp.status_code == 200 and not captcha,
            status_code=resp.status_code,
            text=text,
            error=None,
            captcha=captcha,
        )
    except requests.exceptions.Timeout:
        return FetchResult(ok=False, status_code=None, text=None, error="timeout")
    except requests.exceptions.ConnectionError as e:
        return FetchResult(ok=False, status_code=None, text=None, error=f"connection error: {e}")
    except Exception as e:
        return FetchResult(ok=False, status_code=None, text=None, error=str(e))
    finally:
        _local.resolver = None


def fetch(
    url: str,
    params: dict,
    tor_port: int = 9050,
    timeout: int = 15,
    routing: str = "tor",
    custom_dns: str = "",
    tor_timeout: int | None = None,
) -> FetchResult:
    """Fetch a URL using the specified routing mode.

    routing: "tor"          — always route through Tor; never falls back
             "tor_fallback" — try Tor first, fall back to direct on:
                                (a) connection error (Tor unreachable) — always
                                (b) timeout — only when tor_timeout > 0
                              captcha/429/403 are per-instance, not Tor failures,
                              so they never trigger a fallback
             "direct"       — no proxy
    tor_timeout: Tor-specific request timeout (seconds).
                 > 0 → use as Tor request timeout; on timeout, tor_fallback falls back
                 0   → no Tor-specific timeout (use `timeout`); tor_fallback only falls
                       back on connection errors, not on slow responses
                 None → same as 0
    """
    if routing in ("tor", "tor_fallback"):
        user = circuits.get_credential()
        proxy = f"socks5h://{user}:x@127.0.0.1:{tor_port}"
        proxies = {"http": proxy, "https": proxy}
        t = tor_timeout if (tor_timeout is not None and tor_timeout > 0) else timeout
        result = _do_request(url, params, proxies, t, is_direct=False, custom_dns=custom_dns)
        if routing == "tor_fallback" and not result.ok and result.status_code is None:
            # Fall back unless: timed out AND no timeout was configured (tor_timeout=0/None
            # means "only fall back if Tor is unreachable, not just slow")
            if result.error != "timeout" or (tor_timeout is not None and tor_timeout > 0):
                result = _do_request(url, params, {}, timeout, is_direct=True, custom_dns=custom_dns)
    else:
        result = _do_request(url, params, {}, timeout, is_direct=True, custom_dns=custom_dns)
    return result


def _is_captcha(status: int | None, text: str) -> bool:
    # 429 is a rate-limit, not a captcha — let callers apply a short cooldown
    # 403 is a block, not a captcha — same
    if status == 200:
        if 'class="result' in text:
            return False
        lower = text.lower()
        return any(sig in lower for sig in _CAPTCHA_SIGNALS)
    return False
