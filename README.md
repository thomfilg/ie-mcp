# ie-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6.svg)](#requirements)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](https://modelcontextprotocol.io/)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)
[![Version: 0.1.0](https://img.shields.io/badge/version-0.1.0-blue.svg)](#status)

**Give an LLM the ability to drive a legacy IE-only web application.**

> ⚠️ **Status: alpha / work in progress.** This project is under active development.
> The tool surface and configuration may change between versions, and breaking changes
> can land without notice until `1.0.0`. Issues and PRs welcome — see
> [Development](#development) and [Roadmap](#roadmap).

`ie-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io/) server that
automates **Microsoft Edge in IE mode** (the Trident / legacy document-mode engine) via
the Selenium IEDriver, and exposes it over MCP stdio so clients like **Claude Code**,
**Claude Desktop**, or **Codex** can read, click, fill, and scrape any IE-only intranet
app that nothing else can touch anymore.

The server is **application-agnostic**: it knows nothing about any specific site. You point
it at a target through environment variables and the Edge IE-mode site list, and it gives
the model a Playwright-style toolbox (auto-waiting locators, frame navigation, grid
extraction, readiness probing) tuned for the quirks of the Trident engine.

---

## Why this exists

Modern browser-automation tools (Playwright, Puppeteer, Selenium-on-Chromium) cannot render
IE-only apps — the ones that depend on ActiveX, `document.all`, framesets, VBScript-era
behaviours, or an enforced legacy document mode. Edge's **IE mode** is the last supported way
to run them, but it is notoriously awkward to automate:

- IEDriver attach is flaky (Protected-Mode boundary crossings, "could not find IE window").
- The legacy engine rejects Selenium's modern JS atoms (`el.text`, `Select`, `get_attribute`).
- Old document modes have no `window.JSON`, so naive scripts throw `'JSON' is undefined`.
- Frame-based apps lose state if the session is recreated.

`ie-mcp` works around all of these so the model gets a stable, high-level interface.

---

## Features

- **One long-lived session** kept alive across tool calls, so frame-based apps keep state.
- **Auto-waiting locators** (`id` / `css` / `xpath` / `name` / `link_text` / `tag` / `class`).
- **Nested frame navigation** — address frames-inside-frames with a path like `"3/0"`.
- **Trident-safe primitives** — click, fill, select, hover, key-press, scroll, upload, dialogs,
  all implemented to survive the legacy JS engine.
- **Readiness probing** (`ie_wait_ready`) that waits for every same-origin frame's
  `readyState` to complete *and* the DOM to stop changing — handles slow AJAX/frameset grids.
- **Grid extraction** (`ie_grid`) that heuristically finds the real data table and returns
  header-keyed rows.
- **Screenshots** (full window or single element), JS execution, history navigation, resize.
- **Cross-process single-browser lock** so two MCP clients can't collide on one IE session.
- **Orphan cleanup** — enumerate and kill stray Edge IE-mode / IEDriver processes.
- **No required pip install** — selenium can be loaded from a vendored deps folder.
- **Built-in `--selftest`** to diagnose your setup before wiring up a client.

---

## Requirements

| Component | Notes |
|-----------|-------|
| **Windows** | IE mode only exists on Windows; the process-tracking and policy code use `tasklist`, `taskkill`, PowerShell/CIM and `winreg`. |
| **Microsoft Edge** | With IE mode available (Edge ships it on Windows 10/11). |
| **IEDriverServer.exe** | The Selenium IE driver (e.g. `4.14.0`). |
| **Python 3.8+** | Standard library only, plus selenium. |
| **selenium** | `>=4.14.0` — installed via `requirements.txt` or vendored (see below). |

---

## Installation

Install it as a command, then let it register itself with your MCP clients. **No paths
to type** — `ie-mcp` resolves its own launch path.

```bash
# 1. install (puts an `ie-mcp` command on PATH; pipx/uv keep it isolated)
pipx install git+https://github.com/thomfilg/ie-mcp.git
#   or:  uv tool install git+https://github.com/thomfilg/ie-mcp.git
#   or from a clone:  pipx install .

# 2. register with every MCP client found on PATH (Claude Code, Codex, Gemini CLI)
ie-mcp --install
```

That's it — `ie-mcp --install` runs `claude mcp add` / `codex mcp add` / `gemini mcp add`
for you with the path already solved. Remove it again with `ie-mcp --uninstall`.

### Register a client by hand

`ie-mcp --install` (above) does this for you. To wire one up manually, use the `ie-mcp`
command — it's on PATH after install, so there's still no path to type:

| Client | Command |
|--------|---------|
| **Claude Code** | `claude mcp add ie-mcp -- ie-mcp` |
| **Codex CLI** | `codex mcp add ie-mcp -- ie-mcp` |
| **Gemini CLI** | `gemini mcp add ie-mcp ie-mcp` |

Or via a client's config file (all equivalent — `{ "command": "ie-mcp" }`):

- **Claude Desktop** — `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`)
- **Gemini CLI** — `~/.gemini/settings.json` (Windows: `%USERPROFILE%\.gemini\settings.json`)

```json
{
  "mcpServers": {
    "ie-mcp": { "command": "ie-mcp" }
  }
}
```

- **Codex CLI** — `~/.codex/config.toml` (Windows: `%USERPROFILE%\.codex\config.toml`)

```toml
[mcp_servers.ie-mcp]
command = "ie-mcp"
```

Any other stdio MCP client works too: set the server command to `ie-mcp`. To override a
default, add the env var to that client's `env` block — see [Configuration](#configuration).

> IEDriver attach can be slow on the first call. If a client reports the server as
> unresponsive on startup, give it a longer init timeout and confirm `ie-mcp --selftest`
> passes standalone first.

Download **IEDriverServer.exe** (matching your Edge / selenium version) from the
[Selenium downloads](https://www.selenium.dev/downloads/) page if it isn't already in the
default per-user selenium cache.

### Verify the setup

Before (or after) wiring up a client, run the built-in self-test. It checks dependencies
and paths, then opens and closes a real IE-mode session and confirms the Trident engine is
actually in use:

```bash
ie-mcp --selftest
```

A passing run prints `RESULT: PASS` and an IE/Trident `userAgent`.

---

## Configuration

All configuration is via environment variables — **all optional**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `IE_EDGE_PATH` | `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` | Path to `msedge.exe`. |
| `IE_DRIVER_PATH` | *(per-user cache path)* | Path to `IEDriverServer.exe`. |
| `IE_SITE_LIST` | *(unset)* | Path to an Edge IE-mode site-list XML. If set, the server verifies the HKCU Edge policy points at it so listed sites render in IE mode. If unset, no policy is managed and Edge is assumed already configured. |
| `IE_DEFAULT_URL` | `about:blank` | URL `ie_open` uses when none is given. |
| `IE_ATTACH_RETRIES` | `3` | IEDriver attach attempts. |
| `IE_PAGE_TIMEOUT` | `30` | Per-page load timeout (seconds). |
| `IE_PYDEPS` | `../.pydeps` | Extra `sys.path` dir for vendored selenium. |
| `IE_LOG_FILE` | *(unset)* | If set, also append `[ie-mcp]` logs to this file. |
| `IE_LOCK_FILE` | `%TEMP%\ie_mcp.lock` | Cross-process single-browser lock file. |
| `IE_NO_LOCK` | *(unset)* | Set to `1`/`true`/`yes` to disable the single-browser lock. |


---

## Tools

| Tool | Description |
|------|-------------|
| `ie_open` | Start (or reuse) an Edge IE-mode session and navigate to a URL. |
| `ie_goto` | Navigate the current session to a new URL. |
| `ie_status` | Session state as JSON (active/alive, title, url, owned PIDs). |
| `ie_browsers` | List all Edge IE-mode windows + IEDriver processes, flagging the one this session owns. |
| `ie_kill_orphans` | Terminate IE-mode Edge / IEDriver processes not owned by this session. |
| `ie_frames` | List the frames/iframes of the current page (index, name, id, src). |
| `ie_text` | Get visible text of the page or a specific frame. |
| `ie_html` | Get HTML source of the page or a frame. |
| `ie_click` | Click an element (auto-waits); `click` / `double` / `right`. |
| `ie_fill` | Type text into an input/textarea (auto-waits). |
| `ie_upload` | Set a `<input type=file>` by sending a local path. |
| `ie_dialog` | Handle a JS dialog (`accept` / `dismiss` / `text` / `sendkeys`). |
| `ie_js` | Execute JavaScript in the page or a frame and return the result. |
| `ie_screenshot` | PNG screenshot of the window, or of a single element. |
| `ie_wait_text` | Poll until a substring appears (or disappears) in the page text. |
| `ie_wait_ready` | Wait until the page (and same-origin frames) finish loading and settle. |
| `ie_select` | Select an `<option>` by label / value / index (Trident-safe). |
| `ie_press_key` | Press a key (Enter/Tab/Escape/arrows/… or a literal char). |
| `ie_back` / `ie_forward` | Navigate browser history. |
| `ie_hover` | Hover over an element (menus/tooltips). |
| `ie_resize` | Resize the browser window. |
| `ie_get` | Read an element's text, `value`, or a named attribute (Trident-safe). |
| `ie_wait_element` | Wait for an element to appear — or, with `gone=true`, disappear. |
| `ie_scroll` | Scroll an element into view, to top/bottom, or by pixels. |
| `ie_grid` | Extract a tabular grid as structured (header-keyed) rows. |
| `ie_sleep` | Sleep N seconds (last resort; prefer `ie_wait_ready` / `ie_wait_text`). |
| `ie_close` | Close the session and quit the browser. |

### Working with frames

Many legacy apps are framesets. Use `ie_frames` to enumerate them, then pass `frame` to most
tools. A nested path reaches frames-inside-frames — e.g. `"3/0"` is the first frame inside the
fourth frame.

---

## No-pip / vendored selenium

On locked-down or offline machines you can run without a global `pip install`. Drop the
`selenium` package (and its deps) into a folder and point `IE_PYDEPS` at it:

```bash
pip install --target ../.pydeps "selenium>=4.14.0,<5"
```

By default the server looks for `../.pydeps` relative to `ie_mcp.py`, so the MCP protocol
itself has **zero** pip dependencies (newline-delimited JSON-RPC 2.0 is implemented inline).

---

## How IE mode is reached

When `IE_SITE_LIST` is set, the server self-heals the HKCU Edge policy
(`SOFTWARE\Policies\Microsoft\Edge`) so the listed sites open in IE mode:

- `InternetExplorerIntegrationLevel = 1`
- `InternetExplorerIntegrationSiteList = file:///…/your-site-list.xml`
- `InternetExplorerIntegrationReloadInIEModeAllowed = 1`

These are written under **HKCU** (no admin needed). If the key isn't writable, the server
logs the exact values to set manually. When `IE_SITE_LIST` is unset, the server manages no
policy and assumes Edge is already configured for IE mode.

> Session creation navigates to the target URL **during** session start — the only reliable
> way to cross the Protected-Mode boundary into IE mode without the driver losing the browser.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `could not start Edge IE mode after N tries` | Confirm IE mode renders the site manually in Edge; check `IE_SITE_LIST`; try bumping `IE_ATTACH_RETRIES`. |
| `another ie-mcp process … already owns the IE browser` | A second client tried to start. Close the other one, run `ie_kill_orphans`, or set `IE_NO_LOCK=1`. |
| `IE engine: NO (Chromium?)` in selftest | The site isn't matching the IE-mode site list; the page rendered in Chromium. Fix the site list / policy. |
| Leftover Edge windows after a crash | Use `ie_browsers` to inspect and `ie_kill_orphans` to clean up. |
| `'JSON' is undefined` from your own `ie_js` | The page is in IE7/quirks mode with no `window.JSON`. Return delimited strings instead of `JSON.stringify`. |

---

## Development

This is a single-file server (`ie_mcp.py`). Install it editable so the `ie-mcp` command
points at your working copy:

```bash
git clone https://github.com/thomfilg/ie-mcp.git
cd ie-mcp
pip install -e .          # or: pipx install --editable .

# diagnose deps/paths and open a real IE-mode session
ie-mcp --selftest
```

Running straight from the clone without installing also works — `python ie_mcp.py
--selftest` / `--install` fall back to launching the file by absolute path.

**Project layout**

- `ie_mcp.py` — the entire server: config, the IEDriver wrapper (`IeSession`), one
  `t_*` function per tool, the `TOOLS` registry, the `--install`/`--selftest` CLI,
  and the stdio JSON-RPC loop.
- `pyproject.toml` — packaging + the `ie-mcp` console-script entry point.
- `requirements.txt` — runtime deps (just selenium; mirrors `pyproject.toml`).

**Adding a tool**

1. Write a `t_<name>(args)` function that returns `text_result(...)` (or an image result).
2. Append an entry to the `TOOLS` list with `name`, `description`, `inputSchema`, and `fn`.
3. Keep it **Trident-safe**: prefer direct `arguments[0].innerText` / `getAttribute` over
   Selenium's JS atoms, and never rely on `window.JSON` inside injected scripts (IE7/quirks
   document modes don't have it — return delimited strings and parse them in Python).
4. Re-run `--selftest`, then exercise the tool from an MCP client against a real IE-mode app.

**Debugging**

- Set `IE_LOG_FILE` to capture the `[ie-mcp]` stderr log to a file.
- Use `ie_browsers` / `ie_kill_orphans` to inspect and clean up stray Edge/IEDriver processes.
- Set `IE_NO_LOCK=1` while iterating to bypass the single-browser lock.

### Contributing

Bug reports and PRs are welcome. Please:

- Keep the server **single-file and dependency-light** (selenium is the only runtime dep).
- Match the existing tool conventions (auto-waiting `find`, `frame` arg, `text_result`).
- Note any IE-mode / Trident quirk you worked around in a code comment — they're rarely obvious.

---

## Roadmap

Planned / in progress (subject to change while in alpha):

- [ ] Multi-tab / multiple window-handle support.
- [ ] Cookie and `localStorage` inspection tools.
- [ ] Richer `ie_grid` (pagination + virtualized-grid scrolling helpers).
- [ ] Optional structured (JSON) tool results alongside text.
- [ ] Configurable per-tool default waits.
- [ ] Packaged release + versioned tags once the tool surface stabilizes (`1.0.0`).

---

## License

[MIT](LICENSE) © 2026 thomfilg
