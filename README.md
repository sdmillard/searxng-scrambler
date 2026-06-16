A little about me before reading, I AM NOT A DEVELOPER. I am a political science major who doesn't know jack about coding, I just happen to, for some reason, have, what I think to be, a solid understanding of data privacy routing and wanted to do my part to better searxng as I am a user. I vibe coded quite literally every line of this with claude code over a couple days, this is more of a proof of concept that I think some capable computer nerds can actually bring to fruition. HOWEVER, it should be acceptable to use in its current state, I even use it myself.

# Scrambler

Every search engine knows two things about you: who you are and what you searched for. That combination is the product. Scrambler breaks it.

Scrambler is a local search proxy that routes your queries across multiple independent [SearXNG](https://searxng.github.io/searxng/) instances through the Tor network. No single server ever sees both your IP address and your query at the same time — your identity and your intent stay in separate hands.

---

## How it works

**SearXNG** is an open-source meta-search engine. Volunteers run public instances that query Google, Bing, Brave, DuckDuckGo, and dozens of others on your behalf. Results come back without tracking cookies or personalization. The catch: the instance still knows your IP.

**Tor** routes your traffic through a chain of encrypted relays. The instance sees a Tor exit node, not you. The catch: a single Tor circuit over time builds a pattern.

**Scrambler** combines both and then goes further. Each search fans out across multiple instances simultaneously, each on its own Tor circuit. Results are merged, deduplicated, and ranked. No instance has the full picture. No circuit accumulates a history. The subtitle says it plainly:

> *queries never logged — tor-routed — no single party holds your identity and your query*

---

## Features

- **Multi-instance fanout** — queries hit several instances in parallel; results are merged and ranked by consensus
- **Tor routing** — every instance request goes through a separate Tor circuit by default
- **Smart ranking** — engine consensus, lexical scoring, and optional semantic reranking surface the best results
- **AI on demand** — optional Ask AI button summarizes results using a provider of your choice; never runs automatically
- **Search profiles** — save different configurations for different use cases (research, news, tech, etc.)
- **Themes** — full visual customization including fonts, colors, backgrounds, sound effects, and loading screens
- **Instance autopick** — automatically discovers and benchmarks healthy public instances
- **Desktop integration** — installs as a native app on Linux, macOS, and Windows with its own launcher and autostart support

---

## Requirements

- Python 3.10+
- [Tor](https://www.torproject.org/) running locally on port 9050

To install Tor:

```bash
# Debian / Ubuntu
sudo apt install tor

# Arch
sudo pacman -S tor

# macOS
brew install tor

# Windows — download the Expert Bundle from https://www.torproject.org/download/tor/
# Extract it and run tor.exe
```

Start it and leave it running:

```bash
sudo systemctl enable --now tor   # Linux systemd
brew services start tor           # macOS
tor &                             # foreground (any platform)
```

---

## Installation

```bash
pip install searxng-scrambler
```

To enable local AI reranking (semantic search, runs on your GPU/CPU, no API key needed):

```bash
pip install "searxng-scrambler[ai]"
```

> **Note:** The `[ai]` extra pulls in PyTorch and sentence-transformers (~1–5 GB depending on your platform). Skip it if you only want cloud-based AI via an API key.

Or from source:

```bash
git clone https://github.com/sdmillard/searxng-scrambler
cd searxng-scrambler
pip install -e .
```

---

## Quick start

```bash
scrambler doctor   # check that Tor is running and config is ready
scrambler serve    # start the proxy
```

Open [http://localhost:7777](http://localhost:7777) in your browser. That's it.

`scrambler doctor` checks your Python version, Tor connectivity, all required packages, and your instance list. It exits with a non-zero code if anything is wrong, so you can use it in setup scripts.

On first run, Scrambler creates a config directory at `~/.config/scrambler/`. Add your SearXNG instances there, or use the **Instances** panel in Settings to paste URLs and run the built-in instance discovery tool.

---

## Adding instances

Scrambler needs a list of public SearXNG instances to query. You can:

- Use **Settings → Instances → Discover** to find and benchmark public instances automatically
- Paste URLs manually, one per line, in the instance list
- Start from `instances.example.txt` in this repo

The more instances you add, the better the result diversity and the harder it is for any one party to build a profile.

---

## Configuration

Everything is configurable through the web UI at [http://localhost:7777/settings](http://localhost:7777/settings):

| Section | What it controls |
|---|---|
| **Instances** | Which SearXNG instances to query, autopick scheduling |
| **Routing** | Tor vs direct, circuit warmup, per-service routing |
| **AI** | Provider, model, API key, reranking, image generation |
| **Appearance** | Theme, colors, fonts, backgrounds, sounds |
| **Search Preferences** | Engines, language, result ranking, profiles |
| **Backup & Restore** | Export/import themes, profiles, and all settings |

Settings are saved to `~/.config/scrambler/preferences.json`. API keys are stored server-side only and are never included in exports.

---

## Search profiles

Profiles save your search configuration as a named preset — engines, ranking strategy, AI settings, streaming mode — and let you switch between them instantly. Scrambler ships with built-in profiles for common use cases:

- **Default** — balanced general search
- **Deep Dive** — broad engines, semantic reranking, Jina page fetching
- **Research** — academic sources prioritized, full AI context
- **Tech** — IT category, developer sources boosted
- **News** — freshness over consensus, autopick on every search
- **Privacy** — Tor-strict, no external calls, offline maps, local AI only
- **Performance** — direct routing, single instance, no ranking overhead

---

## Themes

Scrambler's appearance is fully themeable. The Settings page lets you customize colors, fonts, background images or video, cursor, sound effects, loading animations, and more. Themes can be saved, exported as JSON, and shared. The built-in themes include Dark, Light, Nord, Catppuccin, Solarized, Breakfast, and Hacker.

---

## Desktop app

Scrambler can install itself as a native app on Linux, macOS, and Windows. Go to **Settings → System** and use the **Desktop launcher** and **Start on login** buttons.

| Platform | Launcher | Autostart |
|---|---|---|
| **Linux** | `.desktop` entry + SVG icon in your app menu | systemd user service |
| **macOS** | `Scrambler.app` in `~/Applications` (Spotlight-searchable) | launchd user agent |
| **Windows** | Shortcut in Start Menu Programs | Registry startup key |

The launcher starts the server silently in the background and opens a themed loading page in your browser while it connects — no terminal needed. The app name and colors come from your current theme, so reinstall after switching themes to update it.

---

## CLI reference

```
scrambler serve              Start the server (default port: 7777)
  --port PORT                Override port
  --config-dir DIR           Use a different config directory
  --no-tor                   Disable Tor (exposes your real IP)
  --debug                    Flask debug mode

scrambler doctor             Check that dependencies and config are ready
  --config-dir DIR           Check a specific config directory

scrambler discover           Browse and benchmark public SearXNG instances
  --min-grade GRADE          Filter by instance grade (A+, A, B, ...)
```

---

## Privacy notes

- Scrambler runs entirely on your machine. Nothing is sent to any Scrambler server because there is no Scrambler server.
- Your queries go to SearXNG instances through Tor, not to Google or Bing directly.
- API keys (AI providers, etc.) are stored in `~/.config/scrambler/preferences.json` and never leave your machine.
- Search history is stored locally in `~/.config/scrambler/history.json`. You can disable or clear it from Settings.
- Exports explicitly strip API keys. Themes and profiles are safe to share.

---

## Support

[![Redistribute your wealth to me](beg-button.png)](https://ko-fi.com/sdmillard)

---

## License

AGPL-3.0-or-later. If you run a modified version as a service, you must publish the source.
