import os
import shutil
import subprocess
import sys
from pathlib import Path

_SERVICE_NAME = "scrambler"
_SERVICE_FILE = Path.home() / ".config" / "systemd" / "user" / f"{_SERVICE_NAME}.service"


def _bin() -> str:
    return os.path.realpath(sys.argv[0])


def _service_content() -> str:
    return (
        "[Unit]\n"
        "Description=Searxng Scrambler\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart={_bin()} serve\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def available() -> bool:
    return bool(shutil.which("systemctl"))


def is_enabled() -> bool:
    return _SERVICE_FILE.exists()


def enable() -> str | None:
    try:
        _SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SERVICE_FILE.write_text(_service_content())
        r = subprocess.run(
            ["systemctl", "--user", "enable", _SERVICE_NAME],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return r.stderr.strip() or "systemctl enable failed"
        return None
    except Exception as e:
        return str(e)


def disable() -> str | None:
    try:
        subprocess.run(
            ["systemctl", "--user", "disable", _SERVICE_NAME],
            capture_output=True, text=True, timeout=10,
        )
        if _SERVICE_FILE.exists():
            _SERVICE_FILE.unlink()
        return None
    except Exception as e:
        return str(e)
