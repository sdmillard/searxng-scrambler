import argparse
import sys
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "scrambler"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scrambler",
        description="Route SearXNG queries through Tor across multiple instances.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the local search proxy")
    serve.add_argument("--port", type=int, default=None, help="Port to bind (default: 7777, or saved in config)")
    serve.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, metavar="DIR")
    serve.add_argument("--no-tor", action="store_true", help="Disable Tor (exposes your real IP)")
    serve.add_argument("--debug", action="store_true", help="Flask debug mode")

    discover = sub.add_parser("discover", help="Browse public SearXNG instances")
    discover.add_argument("--min-grade", metavar="GRADE", help="Filter to A+/A/B/C/... and above")

    doctor = sub.add_parser("doctor", help="Check that dependencies and config are ready")
    doctor.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, metavar="DIR")

    args = parser.parse_args()

    if args.command == "serve" or args.command is None:
        _serve(args)
    elif args.command == "discover":
        from .discover import discover as _discover
        _discover(min_grade=getattr(args, "min_grade", None))
    elif args.command == "doctor":
        _doctor(args)


def _doctor(args) -> None:
    import socket

    config_dir: Path = getattr(args, "config_dir", DEFAULT_CONFIG_DIR)
    ok = True

    def check(label: str, passed: bool, note: str = "") -> None:
        nonlocal ok
        sym = "  ok" if passed else "FAIL"
        line = f"  [{sym}]  {label}"
        if note:
            line += f"  —  {note}"
        print(line)
        if not passed:
            ok = False

    print()
    print("Scrambler doctor")
    print("─" * 44)

    # Python version
    maj, min_ = sys.version_info[:2]
    check(
        f"Python {maj}.{min_}",
        (maj, min_) >= (3, 10),
        "" if (maj, min_) >= (3, 10) else "Python 3.10+ required",
    )

    # Tor SOCKS5
    tor_ok = False
    try:
        s = socket.create_connection(("127.0.0.1", 9050), timeout=2)
        s.close()
        tor_ok = True
    except OSError:
        pass
    check(
        "Tor (127.0.0.1:9050)",
        tor_ok,
        "" if tor_ok else "install tor and start it  →  sudo systemctl start tor",
    )

    # Core pip packages
    for pkg, import_name in [
        ("flask", "flask"),
        ("requests", "requests"),
        ("beautifulsoup4", "bs4"),
        ("lxml", "lxml"),
        ("dnspython", "dns"),
        ("markdown", "markdown"),
    ]:
        try:
            __import__(import_name)
            avail = True
        except ImportError:
            avail = False
        check(
            f"pip: {pkg}",
            avail,
            "" if avail else f"pip install {pkg}",
        )

    # Optional AI reranking
    try:
        import sentence_transformers  # noqa: F401
        ai_ok = True
    except ImportError:
        ai_ok = False
    check(
        "AI reranking (sentence-transformers)",
        ai_ok,
        "" if ai_ok else "optional  →  pip install 'searxng-scrambler[ai]'",
    )

    # Config dir
    check(
        f"Config dir  {config_dir}",
        config_dir.exists(),
        "" if config_dir.exists() else "will be created on first  scrambler serve",
    )

    # Instance list
    inst_file = config_dir / "instances.txt"
    if inst_file.exists():
        lines = [l.strip() for l in inst_file.read_text().splitlines() if l.strip() and not l.startswith("#")]
        check(
            f"Instances ({len(lines)} configured)",
            len(lines) > 0,
            "" if lines else f"add instances to {inst_file}  or run  scrambler discover",
        )
    else:
        check(
            "Instance list",
            False,
            f"run  scrambler discover  or add URLs to {inst_file}",
        )

    print("─" * 44)
    if ok:
        print("  All checks passed. Run  scrambler serve  to start.")
    else:
        print("  Some checks failed — see notes above.")
    print()
    sys.exit(0 if ok else 1)


def _serve(args) -> None:
    from .server import create_app
    from .config import _load_prefs

    config_dir: Path = getattr(args, "config_dir", DEFAULT_CONFIG_DIR)
    config_dir.mkdir(parents=True, exist_ok=True)
    no_tor: bool = getattr(args, "no_tor", False)
    debug: bool = getattr(args, "debug", False)

    cli_port = getattr(args, "port", None)
    if cli_port is not None:
        port = cli_port
    else:
        prefs = _load_prefs(config_dir / "preferences.json")
        port = prefs.get("server_port", 7777)

    print(f"[Scrambler] Config dir : {config_dir}")
    print(f"[Scrambler] Starting   : http://127.0.0.1:{port}")
    if not no_tor:
        print(f"[Scrambler] Tor SOCKS5 : 127.0.0.1:9050")

    app = create_app(config_dir=config_dir, no_tor=no_tor)
    app.run(host="127.0.0.1", port=port, debug=debug, threaded=True)
