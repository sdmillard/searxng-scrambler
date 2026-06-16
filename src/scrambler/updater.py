import json, subprocess, sys, threading, time, urllib.request
from . import __version__

_wake  = threading.Event()
_lock  = threading.Lock()
status: dict = {
    "current":      __version__,
    "latest":       None,
    "available":    False,
    "last_checked": None,
    "error":        None,
    "updating":     False,
}
_CACHE_TTL = 3600


def _parse_ver(v: str):
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


def check() -> dict:
    now = time.time()
    with _lock:
        if status["latest"] and status["last_checked"] and now - status["last_checked"] < _CACHE_TTL:
            return dict(status)
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/searxng-scrambler/json",
            headers={"User-Agent": f"searxng-scrambler/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        latest = data["info"]["version"]
        with _lock:
            status.update(
                current=__version__, latest=latest,
                available=_parse_ver(latest) > _parse_ver(__version__),
                last_checked=time.time(), error=None,
            )
    except Exception as e:
        with _lock:
            status.update(error=str(e), last_checked=time.time())
    return dict(status)


def apply() -> tuple[bool, str]:
    with _lock:
        status["updating"] = True
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "searxng-scrambler"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, (result.stderr or result.stdout).strip()
    except Exception as e:
        return False, str(e)
    finally:
        with _lock:
            status["updating"] = False


def _check_and_apply(config, restart_fn) -> None:
    info = check()
    if info.get("available"):
        ok, out = apply()
        if ok:
            print(f"[Scrambler] Updated to {info['latest']}, restarting…", file=sys.stderr)
            restart_fn()
        else:
            print(f"[Scrambler] Auto-update failed: {out}", file=sys.stderr)


def start_scheduler(config, restart_fn) -> None:
    def _loop():
        if config.prefs.get("auto_update", "off") == "startup":
            _check_and_apply(config, restart_fn)
        while True:
            schedule = config.prefs.get("auto_update", "off")
            sleep_s = (
                max(5, int(config.prefs.get("auto_update_interval", 60))) * 60
                if schedule == "interval" else 3600
            )
            _wake.wait(timeout=sleep_s)
            _wake.clear()
            if config.prefs.get("auto_update", "off") == "interval":
                _check_and_apply(config, restart_fn)

    threading.Thread(target=_loop, daemon=True, name="update-scheduler").start()
