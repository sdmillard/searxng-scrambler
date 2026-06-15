import json
import os
import subprocess
import sys
from pathlib import Path

_APP_ID = "scrambler"
_APP_NAME = "Scrambler"
_ICON_DIR = Path.home() / ".local/share/icons/hicolor/scalable/apps"
_DESKTOP_DIR = Path.home() / ".local/share/applications"
_BIN_DIR = Path.home() / ".local/bin"

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


def is_installed() -> bool:
    return (_DESKTOP_DIR / f"{_APP_ID}.desktop").exists()


def create(config_dir: Path, appearance: dict | None = None) -> str | None:
    try:
        port = _port(config_dir)
        bin_path = _bin()
        app = appearance or {}
        colors = app.get("colors") or {}

        name    = (app.get("title") or _APP_NAME).strip() or _APP_NAME
        favicon = (app.get("favicon") or "🔍").strip() or "🔍"
        bg      = colors.get("--bg", "#0d1117")
        accent  = colors.get("--accent", "#5b9cf6")

        # Icon
        _ICON_DIR.mkdir(parents=True, exist_ok=True)
        (_ICON_DIR / f"{_APP_ID}.svg").write_text(_icon_svg(favicon, bg, accent))

        # Loading page
        loading_path = config_dir / "loading.html"
        loading_path.write_text(_loading_html(port, app))

        # Launcher script
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

        # .desktop entry
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


def remove() -> str | None:
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
