import base64
import json
import os
import subprocess
import sys
from pathlib import Path

_APP_ID   = "scrambler"
_APP_NAME = "Scrambler"

# Linux paths
_ICON_DIR    = Path.home() / ".local/share/icons/hicolor/scalable/apps"
_DESKTOP_DIR = Path.home() / ".local/share/applications"
_BIN_DIR     = Path.home() / ".local/bin"

# macOS paths
_MAC_APPS_DIR   = Path.home() / "Applications"
_MAC_APP_BUNDLE = _MAC_APPS_DIR / f"{_APP_NAME}.app"

_DEFAULT_QUIPS = [
    "Connecting to Tor network...",
    "Routing through the onion...",
    "Warming up circuits...",
    "Scrambling your identity...",
    "Shuffling instances...",
    "Establishing anonymous connection...",
    "Privacy engine loading...",
    "Almost there...",
]


def _bin() -> str:
    return os.path.realpath(sys.argv[0])


def _port(config_dir: Path) -> int:
    try:
        data = json.loads((config_dir / "preferences.json").read_text())
        return int(data.get("server_port", 7777))
    except Exception:
        return 7777


def _icon_svg(favicon: str = "🔍", bg: str = "#0d1117", accent: str = "#5b9cf6") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">\n'
        f'  <rect width="256" height="256" rx="56" fill="{bg}"/>\n'
        f'  <text x="128" y="182" text-anchor="middle" font-size="148"'
        f' font-family="Noto Color Emoji,Apple Color Emoji,Segoe UI Emoji,sans-serif"'
        f'>{favicon}</text>\n'
        '</svg>\n'
    )


def _loading_html(port: int, app: dict) -> str:
    colors = app.get("colors") or {}
    bg      = colors.get("--bg",      "#0d1117")
    surface = colors.get("--surface", "#1c1c1c")
    accent  = colors.get("--accent",  "#5b9cf6")
    text    = colors.get("--text",    "#d8d8d8")
    muted   = colors.get("--muted",   "#666666")
    title   = (app.get("title") or _APP_NAME).strip() or _APP_NAME
    quips   = app.get("loading_quips") or _DEFAULT_QUIPS
    quips_js = json.dumps(quips)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Starting {title}...</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  html,body{{height:100%;background:{bg};color:{text};font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center}}
  .wrap{{text-align:center;width:360px;padding:0 20px}}
  .brand{{font-size:38px;font-weight:700;color:{accent};margin-bottom:36px;letter-spacing:-0.5px}}
  .quip{{font-size:14px;color:{muted};min-height:22px;margin-bottom:24px;transition:opacity 0.35s}}
  .quip.hidden{{opacity:0}}
  .track{{height:4px;background:{surface};border-radius:4px;overflow:hidden}}
  .fill{{height:100%;background:{accent};border-radius:4px;width:0%;transition:width 0.5s ease-out}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">{title}</div>
  <div class="quip" id="quip"> </div>
  <div class="track"><div class="fill" id="fill"></div></div>
</div>
<script>
const PORT = {port};
const QUIPS = {quips_js};
let qi = 0, pct = 0, done = false;
const fill = document.getElementById('fill');
const quipEl = document.getElementById('quip');

function nextQuip() {{
  quipEl.classList.add('hidden');
  setTimeout(() => {{
    quipEl.textContent = QUIPS[qi % QUIPS.length];
    qi++;
    quipEl.classList.remove('hidden');
  }}, 350);
}}

function tick() {{
  if (done) return;
  pct = pct + (90 - pct) * 0.07;
  fill.style.width = pct + '%';
}}

nextQuip();
setInterval(nextQuip, 2600);
setInterval(tick, 400);

async function poll() {{
  try {{
    await fetch('http://localhost:' + PORT + '/', {{mode:'no-cors'}});
    done = true;
    fill.style.width = '100%';
    setTimeout(() => {{ window.location.href = 'http://localhost:' + PORT; }}, 450);
  }} catch(e) {{
    setTimeout(poll, 500);
  }}
}}
setTimeout(poll, 400);
</script>
</body>
</html>"""


# ── Windows helpers ───────────────────────────────────────────────────────────

def _win_start_menu() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _win_script_dir() -> Path:
    localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(localappdata) / "Scrambler"


def _win_launcher_script(config_dir: Path, port: int) -> str:
    bin_path  = _bin()
    log_path  = str(_win_script_dir() / "scrambler.log")
    load_path = str(config_dir / "loading.html").replace("\\", "/")
    return (
        "import subprocess, socket, webbrowser, os\n"
        "CREATE_NO_WINDOW = 0x08000000\n"
        f"_BIN  = {repr(bin_path)}\n"
        f"_URL  = {repr('file:///' + load_path)}\n"
        f"_LOG  = {repr(log_path)}\n"
        f"_PORT = {port}\n"
        "\n"
        "def _running():\n"
        "    try:\n"
        "        s = socket.create_connection(('127.0.0.1', _PORT), timeout=1)\n"
        "        s.close(); return True\n"
        "    except OSError:\n"
        "        return False\n"
        "\n"
        "if not _running():\n"
        "    os.makedirs(os.path.dirname(_LOG), exist_ok=True)\n"
        "    with open(_LOG, 'a') as log:\n"
        "        subprocess.Popen([_BIN, 'serve'], creationflags=CREATE_NO_WINDOW,\n"
        "                         stdout=log, stderr=log)\n"
        "\n"
        "webbrowser.open(_URL)\n"
    )


def _win_create_shortcut(shortcut_path: Path, target: str, arguments: str) -> str | None:
    ps = "\n".join([
        "$ws = New-Object -ComObject WScript.Shell",
        f'$s = $ws.CreateShortcut("{shortcut_path}")',
        f'$s.TargetPath = "{target}"',
        f'$s.Arguments = \'"{arguments}"\'',
        f'$s.WorkingDirectory = "{Path.home()}"',
        "$s.Save()",
    ])
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return r.stderr.strip() or "PowerShell shortcut creation failed"
        return None
    except Exception as e:
        return str(e)


# ── Public interface ──────────────────────────────────────────────────────────

def is_installed() -> bool:
    if sys.platform == "win32":
        return (_win_start_menu() / f"{_APP_NAME}.lnk").exists()
    if sys.platform == "darwin":
        return _MAC_APP_BUNDLE.exists()
    return (_DESKTOP_DIR / f"{_APP_ID}.desktop").exists()


def create(config_dir: Path, appearance: dict | None = None) -> str | None:
    if sys.platform == "win32":
        return _create_windows(config_dir, appearance)
    if sys.platform == "darwin":
        return _create_mac(config_dir, appearance)
    return _create_linux(config_dir, appearance)


def remove() -> str | None:
    if sys.platform == "win32":
        return _remove_windows()
    if sys.platform == "darwin":
        return _remove_mac()
    return _remove_linux()


# ── Linux implementation ──────────────────────────────────────────────────────

def _create_linux(config_dir: Path, appearance: dict | None = None) -> str | None:
    try:
        port = _port(config_dir)
        bin_path = _bin()
        app = appearance or {}
        colors = app.get("colors") or {}

        name    = (app.get("title") or _APP_NAME).strip() or _APP_NAME
        favicon = (app.get("favicon") or "🔍").strip() or "🔍"
        bg      = colors.get("--bg", "#0d1117")
        accent  = colors.get("--accent", "#5b9cf6")

        _ICON_DIR.mkdir(parents=True, exist_ok=True)
        (_ICON_DIR / f"{_APP_ID}.svg").write_text(_icon_svg(favicon, bg, accent))

        loading_path = config_dir / "loading.html"
        loading_path.write_text(_loading_html(port, app))

        _BIN_DIR.mkdir(parents=True, exist_ok=True)
        script = _BIN_DIR / f"{_APP_ID}-open"
        script.write_text(
            f'#!/bin/bash\n'
            f'if ! pgrep -f "scrambler serve" > /dev/null 2>&1; then\n'
            f'    nohup "{bin_path}" serve > /tmp/scrambler.log 2>&1 &\n'
            f'fi\n'
            f'xdg-open "file://{loading_path}"\n'
        )
        script.chmod(0o755)

        _DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
        (_DESKTOP_DIR / f"{_APP_ID}.desktop").write_text(
            f"[Desktop Entry]\n"
            f"Version=1.0\n"
            f"Type=Application\n"
            f"Name={name}\n"
            f"Comment=Private search proxy through Tor\n"
            f"Exec={script}\n"
            f"Icon={_APP_ID}\n"
            f"Terminal=false\n"
            f"Categories=Network;\n"
            f"Keywords=search;privacy;tor;\n"
            f"StartupNotify=true\n"
        )

        for cmd in (
            ["update-desktop-database", str(_DESKTOP_DIR)],
            ["gtk-update-icon-cache", "-f", "-t", str(Path.home() / ".local/share/icons/hicolor")],
        ):
            try:
                subprocess.run(cmd, capture_output=True, timeout=5)
            except Exception:
                pass

        return None
    except Exception as e:
        return str(e)


def _remove_linux() -> str | None:
    try:
        for path in [
            _DESKTOP_DIR / f"{_APP_ID}.desktop",
            _BIN_DIR / f"{_APP_ID}-open",
            _ICON_DIR / f"{_APP_ID}.svg",
        ]:
            if path.exists():
                path.unlink()
        try:
            subprocess.run(["update-desktop-database", str(_DESKTOP_DIR)], capture_output=True, timeout=5)
        except Exception:
            pass
        return None
    except Exception as e:
        return str(e)


# ── macOS implementation ──────────────────────────────────────────────────────

def _create_mac(config_dir: Path, appearance: dict | None = None) -> str | None:
    try:
        port = _port(config_dir)
        bin_path = _bin()
        app = appearance or {}

        name = (app.get("title") or _APP_NAME).strip() or _APP_NAME

        loading_path = config_dir / "loading.html"
        loading_path.write_text(_loading_html(port, app))

        macos_dir = _MAC_APP_BUNDLE / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True, exist_ok=True)

        script = macos_dir / "scrambler-open"
        script.write_text(
            f'#!/bin/bash\n'
            f'if ! pgrep -f "scrambler serve" > /dev/null 2>&1; then\n'
            f'    nohup "{bin_path}" serve > /tmp/scrambler.log 2>&1 &\n'
            f'fi\n'
            f'open "file://{loading_path}"\n'
        )
        script.chmod(0o755)

        (_MAC_APP_BUNDLE / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
            ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '    <key>CFBundleExecutable</key><string>scrambler-open</string>\n'
            '    <key>CFBundleIdentifier</key><string>com.scrambler.app</string>\n'
            f'    <key>CFBundleName</key><string>{name}</string>\n'
            '    <key>CFBundleVersion</key><string>1.0</string>\n'
            '    <key>LSUIElement</key><true/>\n'
            '</dict></plist>\n'
        )

        return None
    except Exception as e:
        return str(e)


def _remove_mac() -> str | None:
    try:
        if _MAC_APP_BUNDLE.exists():
            import shutil
            shutil.rmtree(_MAC_APP_BUNDLE)
        return None
    except Exception as e:
        return str(e)


# ── Windows implementation ────────────────────────────────────────────────────

def _create_windows(config_dir: Path, appearance: dict | None = None) -> str | None:
    try:
        port = _port(config_dir)
        app  = appearance or {}

        loading_path = config_dir / "loading.html"
        loading_path.write_text(_loading_html(port, app))

        script_dir = _win_script_dir()
        script_dir.mkdir(parents=True, exist_ok=True)
        script_path = script_dir / "scrambler-open.pyw"
        script_path.write_text(_win_launcher_script(config_dir, port))

        pythonw = Path(sys.executable).parent / "pythonw.exe"
        if not pythonw.exists():
            pythonw = Path(sys.executable)

        start_menu = _win_start_menu()
        start_menu.mkdir(parents=True, exist_ok=True)
        shortcut = start_menu / f"{_APP_NAME}.lnk"

        return _win_create_shortcut(shortcut, str(pythonw), str(script_path))
    except Exception as e:
        return str(e)


def _remove_windows() -> str | None:
    try:
        shortcut = _win_start_menu() / f"{_APP_NAME}.lnk"
        if shortcut.exists():
            shortcut.unlink()
        script = _win_script_dir() / "scrambler-open.pyw"
        if script.exists():
            script.unlink()
        return None
    except Exception as e:
        return str(e)
