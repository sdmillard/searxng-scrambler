"""
Pre-warmed Tor circuit pool.

Builds circuits in the background by making a lightweight request through each
credential before it's needed. When get_credential() is called during a search,
it returns a ready credential (circuit already established) instead of a fresh
one that Tor still has to build on the spot.

Privacy trade-off: Tor makes outbound connections at rest, not just when you
search. See the settings disclaimer for the full explanation.
"""
import queue
import secrets
import threading

import requests

_WARMUP_URL = "https://check.torproject.org/"
_UA = "Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0"

_pool: queue.Queue = queue.Queue()
_enabled: bool = False
_tor_port: int = 9050
_pool_size: int = 1


def start(tor_port: int = 9050, pool_size: int = 1) -> None:
    global _enabled, _tor_port, _pool_size
    _tor_port = tor_port
    _pool_size = max(1, pool_size)
    _enabled = True
    threading.Thread(target=_fill_pool, daemon=True, name="circuit-warmer").start()


def stop() -> None:
    global _enabled
    _enabled = False
    while not _pool.empty():
        try:
            _pool.get_nowait()
        except queue.Empty:
            break


def get_credential() -> str:
    if not _enabled:
        return secrets.token_hex(8)
    try:
        cred = _pool.get_nowait()
        threading.Thread(target=_refill_one, daemon=True, name="circuit-refill").start()
        return cred
    except queue.Empty:
        return secrets.token_hex(8)  # pool not ready yet, fall back to fresh circuit


def _warm_one() -> str | None:
    cred = secrets.token_hex(8)
    proxy = f"socks5h://{cred}:x@127.0.0.1:{_tor_port}"
    try:
        requests.get(
            _WARMUP_URL,
            proxies={"http": proxy, "https": proxy},
            timeout=(12, 5),
            headers={"User-Agent": _UA},
        )
        return cred
    except Exception:
        return None


def _fill_pool() -> None:
    while _enabled and _pool.qsize() < _pool_size:
        cred = _warm_one()
        if cred and _enabled:
            _pool.put(cred)


def _refill_one() -> None:
    if not _enabled:
        return
    cred = _warm_one()
    if cred and _enabled:
        _pool.put(cred)
