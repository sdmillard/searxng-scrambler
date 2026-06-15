import os
import shutil
import subprocess
import sys
from pathlib import Path

_SERVICE_NAME    = "scrambler"
_SERVICE_FILE    = Path.home() / ".config" / "systemd" / "user" / f"{_SERVICE_NAME}.service"
_MAC_PLIST_LABEL = "com.scrambler.app"
_MAC_PLIST_FILE  = Path.home() / "Library" / "LaunchAgents" / f"{_MAC_PLIST_LABEL}.plist"
_WIN_REG_KEY     = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_REG_VAL     = "Scrambler"


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


def _mac_plist_content() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'    <key>Label</key><string>{_MAC_PLIST_LABEL}</string>\n'
        f'    <key>ProgramArguments</key>\n'
        f'    <array><string>{_bin()}</string><string>serve</string></array>\n'
        f'    <key>RunAtLoad</key><true/>\n'
        f'    <key>KeepAlive</key><true/>\n'
        '</dict>\n'
        '</plist>\n'
    )


def available() -> bool:
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return bool(shutil.which("launchctl"))
    return bool(shutil.which("systemctl"))


def is_enabled() -> bool:
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_REG_KEY, 0, winreg.KEY_QUERY_VALUE)
            winreg.QueryValueEx(key, _WIN_REG_VAL)
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False
    if sys.platform == "darwin":
        return _MAC_PLIST_FILE.exists()
    return _SERVICE_FILE.exists()


def enable() -> str | None:
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_REG_KEY, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, _WIN_REG_VAL, 0, winreg.REG_SZ, f'"{_bin()}" serve')
            winreg.CloseKey(key)
            return None
        except Exception as e:
            return str(e)
    if sys.platform == "darwin":
        try:
            _MAC_PLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            _MAC_PLIST_FILE.write_text(_mac_plist_content())
            r = subprocess.run(
                ["launchctl", "load", str(_MAC_PLIST_FILE)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return r.stderr.strip() or "launchctl load failed"
            return None
        except Exception as e:
            return str(e)
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
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_REG_KEY, 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(key, _WIN_REG_VAL)
            winreg.CloseKey(key)
            return None
        except FileNotFoundError:
            return None
        except Exception as e:
            return str(e)
    if sys.platform == "darwin":
        try:
            if _MAC_PLIST_FILE.exists():
                subprocess.run(
                    ["launchctl", "unload", str(_MAC_PLIST_FILE)],
                    capture_output=True, text=True, timeout=10,
                )
                _MAC_PLIST_FILE.unlink()
            return None
        except Exception as e:
            return str(e)
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
