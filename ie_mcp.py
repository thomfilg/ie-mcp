#!/usr/bin/env python3
"""
IE-mode MCP server.

Drives Microsoft Edge in IE mode (Trident / legacy document mode) via the
Selenium IEDriver, and exposes it over the MCP stdio protocol so MCP clients
(Claude Code, Codex, ...) can automate any legacy IE-only web application.

This server is application-agnostic: it knows nothing about any specific site.
Point it at a target via configuration (env vars) and the IE-mode site list.

Full docs (tools, gotchas): scripts/README_ie_mcp.md

Configuration (all via env vars, all optional):
  IE_EDGE_PATH      path to msedge.exe
  IE_DRIVER_PATH    path to IEDriverServer.exe
  IE_SITE_LIST      path to an Edge IE-mode site list XML. If set, the server
                    verifies the HKCU Edge policy points at it (so listed sites
                    render in IE mode). If unset, the server manages no policy
                    and assumes Edge is already configured for IE mode.
  IE_DEFAULT_URL    URL ie_open uses when none is given (default about:blank)
  IE_ATTACH_RETRIES IEDriver attach attempts (default 3)
  IE_PAGE_TIMEOUT   per-page load timeout in seconds (default 30)
  IE_PYDEPS         extra sys.path dir for vendored selenium (default ../.pydeps)

Design notes:
- One long-lived Selenium session is kept alive across tool calls (the MCP
  process stays running), so frame-based apps keep their state.
- IEDriver attach is flaky (Protected Mode boundary crossings, "could not find
  IE window"), so session creation is retried.
- No pip dependencies: selenium is loaded from a vendored deps folder and the
  MCP protocol (newline-delimited JSON-RPC 2.0) is implemented inline.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback

# --- make vendored selenium importable regardless of how we're launched ------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYDEPS = os.environ.get("IE_PYDEPS", os.path.join(ROOT, ".pydeps"))
if PYDEPS and PYDEPS not in sys.path:
    sys.path.insert(0, PYDEPS)

# --- configuration (overridable via env) -------------------------------------
EDGE = os.environ.get(
    "IE_EDGE_PATH",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
IE_DRIVER = os.environ.get(
    "IE_DRIVER_PATH",
    r"C:\Users\tigre\.cache\selenium\IEDriverServer\win32\4.14.0\IEDriverServer.exe",
)
SITE_LIST = os.environ.get("IE_SITE_LIST")  # optional; no app-specific default
DEFAULT_URL = os.environ.get("IE_DEFAULT_URL", "about:blank")
ATTACH_RETRIES = int(os.environ.get("IE_ATTACH_RETRIES", "3"))
PAGE_TIMEOUT = int(os.environ.get("IE_PAGE_TIMEOUT", "30"))
LOG_FILE = os.environ.get("IE_LOG_FILE")
LOCK_FILE = os.environ.get("IE_LOCK_FILE", os.path.join(tempfile.gettempdir(), "ie_mcp.lock"))
USE_LOCK = os.environ.get("IE_NO_LOCK", "").lower() not in ("1", "true", "yes")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ie-mcp", "version": "0.1.0"}


def log(*a):
    msg = "[ie-mcp] " + " ".join(str(x) for x in a)
    print(msg, file=sys.stderr, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
        except Exception:
            pass


# --- cross-process single-browser lock (Codex + Claude must not collide) ------
def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        return str(pid) in out
    except Exception:
        return False


def acquire_lock():
    """Refuse to start a 2nd concurrent IE session machine-wide (single browser)."""
    if not USE_LOCK:
        return
    if os.path.exists(LOCK_FILE):
        holder = None
        try:
            holder = int(open(LOCK_FILE).read().strip().split()[0])
        except Exception:
            holder = None
        if holder and holder != os.getpid() and _pid_alive(holder):
            raise RuntimeError(
                f"another ie-mcp process (PID {holder}) already owns the IE browser. "
                f"Close it, run ie_kill_orphans there, or set IE_NO_LOCK=1 to override.")
    try:
        with open(LOCK_FILE, "w") as fh:
            fh.write(str(os.getpid()))
    except Exception as exc:
        log(f"WARN could not write lock file: {exc}")


def release_lock():
    if not USE_LOCK:
        return
    try:
        if os.path.exists(LOCK_FILE) and open(LOCK_FILE).read().strip().startswith(str(os.getpid())):
            os.remove(LOCK_FILE)
    except Exception:
        pass


# --- ensure Edge IE-mode policies are set (self-healing, HKCU = no admin) -----
def ensure_ie_policies():
    if not SITE_LIST:
        log("IE_SITE_LIST not set; not managing Edge IE-mode policy")
        return
    try:
        import winreg
    except ImportError:
        return
    key_path = r"SOFTWARE\Policies\Microsoft\Edge"
    site_url = "file:///" + os.path.abspath(SITE_LIST).replace("\\", "/")
    wanted = {
        "InternetExplorerIntegrationLevel": (winreg.REG_DWORD, 1),
        "InternetExplorerIntegrationSiteList": (winreg.REG_SZ, site_url),
        "InternetExplorerIntegrationReloadInIEModeAllowed": (winreg.REG_DWORD, 1),
    }
    # 1) read-only check: if policies are already correct, do nothing.
    current = {}
    try:
        rk = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        for name in wanted:
            try:
                current[name], _ = winreg.QueryValueEx(rk, name)
            except FileNotFoundError:
                current[name] = None
        winreg.CloseKey(rk)
    except FileNotFoundError:
        current = {name: None for name in wanted}
    except OSError:
        current = None

    if current is not None and all(current.get(n) == v for n, (t, v) in wanted.items()):
        log("IE-mode policies OK")
        return

    # 2) something is missing/wrong -> try to write (may need elevation).
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        for name, (typ, val) in wanted.items():
            winreg.SetValueEx(key, name, 0, typ, val)
            log(f"policy set {name} = {val}")
        winreg.CloseKey(key)
    except OSError as exc:
        log(f"WARN IE-mode policy needs updating but the key is not writable "
            f"({exc}). Run once elevated, or set HKCU\\{key_path} manually: "
            f"InternetExplorerIntegrationLevel=1 (DWORD), "
            f"InternetExplorerIntegrationSiteList={site_url}")


# --- process tracking (which Edge IE-mode windows are open) ------------------
def list_ie_browsers():
    """Enumerate all Edge IE-mode windows + IEDriverServer processes system-wide.

    Returns a list of dicts: {pid, kind, profile, url}. Identifies IE-mode Edge by
    the --ie-mode-force flag and pulls the per-session IEDriver-<uuid> profile and
    the launch URL from the command line. Uses PowerShell/CIM (Windows-only).
    """
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='IEDriverServer.exe'\" |"
        " ForEach-Object { $_.Name + '|' + $_.ProcessId + '|' + ($_.CommandLine -replace '[\\r\\n]',' ') }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception as exc:
        log(f"list_ie_browsers failed: {exc}")
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        name, pid = parts[0].strip(), parts[1].strip()
        cmd = parts[2] if len(parts) > 2 else ""
        if not pid.isdigit():
            continue
        pid = int(pid)
        if name.lower() == "iedriverserver.exe":
            rows.append({"pid": pid, "kind": "iedriver", "profile": None, "url": None})
            continue
        # only the main IE-mode browser process (skip helper --type=... procs)
        if "--ie-mode-force" not in cmd or "--type=" in cmd:
            continue
        prof = re.search(r"IEDriver-([0-9a-fA-F-]+)", cmd)
        url = re.search(r"(https?://\S+)", cmd)
        rows.append({"pid": pid, "kind": "edge-ie",
                     "profile": prof.group(1) if prof else None,
                     "url": url.group(1) if url else None})
    return rows


def kill_pids(pids):
    killed = []
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=15)
            killed.append(pid)
        except Exception as exc:
            log(f"kill {pid} failed: {exc}")
    return killed


# --- the IE driver wrapper ---------------------------------------------------
class IeSession:
    def __init__(self):
        self.driver = None
        self.driver_pid = None    # IEDriverServer.exe PID
        self.edge_pid = None      # the IE-mode msedge.exe window PID we own
        self.profile = None       # IEDriver-<uuid> temp profile that identifies us

    def _build(self, initial_url):
        from selenium import webdriver
        from selenium.webdriver.ie.service import Service

        options = webdriver.IeOptions()
        options.attach_to_edge_chrome = True
        options.edge_executable_path = EDGE
        options.ignore_zoom_level = True
        options.ignore_protected_mode_settings = True
        options.require_window_focus = True
        options.ensure_clean_session = True
        options.native_events = True
        options.browser_attach_timeout = 30_000
        options.page_load_strategy = "none"
        # CRITICAL for IE mode: navigate during session creation so IEDriver
        # crosses the Protected-Mode boundary and reconnects to the new browser
        # process *before* handing back control. Navigating afterwards makes the
        # driver lose (and exit on) the browser.
        if initial_url:
            options.initial_browser_url = initial_url

        service = Service(executable_path=IE_DRIVER)
        return webdriver.Ie(service=service, options=options)

    def ensure(self, initial_url=None):
        """Return a live driver, (re)creating it with retries if needed.

        When a new session must be created, initial_url is opened as part of
        session creation (the only reliable way into IE mode).
        """
        from selenium.common.exceptions import WebDriverException

        if self.driver is not None:
            try:
                _ = self.driver.current_url  # cheap liveness probe
                return self.driver
            except WebDriverException:
                log("session dead, recreating")
                self.quit()

        acquire_lock()  # refuse if another ie-mcp process owns the browser
        last = None
        for attempt in range(1, ATTACH_RETRIES + 1):
            try:
                log(f"creating IE-mode session (attempt {attempt}/{ATTACH_RETRIES}) url={initial_url}")
                before = {b["profile"] for b in list_ie_browsers() if b["kind"] == "edge-ie"}
                self.driver = self._build(initial_url)
                self.driver.set_page_load_timeout(PAGE_TIMEOUT)
                _ = self.driver.current_url  # verify it survived the boundary crossing
                self._capture_identity(before)
                return self.driver
            except Exception as exc:
                last = exc
                log(f"attach failed: {exc}")
                self.quit()
                time.sleep(2)
        raise RuntimeError(f"could not start Edge IE mode after {ATTACH_RETRIES} tries: {last}")

    def _capture_identity(self, before_profiles):
        """Record which browser we just opened (driver pid + the new IE-mode edge)."""
        try:
            self.driver_pid = self.driver.service.process.pid
        except Exception:
            self.driver_pid = None
        self.edge_pid = self.profile = None
        try:
            for b in list_ie_browsers():
                if b["kind"] == "edge-ie" and b["profile"] not in before_profiles:
                    self.edge_pid, self.profile = b["pid"], b["profile"]
                    break
        except Exception:
            pass
        log(f"session identity: driver_pid={self.driver_pid} edge_pid={self.edge_pid} profile={self.profile}")

    def quit(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        # IEDriver often leaves the IE-mode Edge window alive — kill ours by pid.
        if self.edge_pid:
            kill_pids([self.edge_pid])
        self.driver_pid = self.edge_pid = self.profile = None
        release_lock()

    # -- helpers --
    def _by(self, by):
        from selenium.webdriver.common.by import By
        m = {
            "id": By.ID, "css": By.CSS_SELECTOR, "xpath": By.XPATH,
            "name": By.NAME, "link_text": By.LINK_TEXT, "tag": By.TAG_NAME,
            "class": By.CLASS_NAME,
        }
        if by not in m:
            raise ValueError(f"unknown 'by': {by} (use one of {sorted(m)})")
        return m[by]

    def _apply_frame(self, frame):
        """Switch into a frame. Accepts a single index/name, or a nested PATH —
        a list, or a string with '/' separators (e.g. "3/0" or "3/inner") — to reach
        frames inside frames, e.g. "3/0" = the 1st frame inside the 4th frame."""
        d = self.driver
        d.switch_to.default_content()
        if frame is None or frame == "":
            return
        if isinstance(frame, (list, tuple)):
            segments = list(frame)
        elif isinstance(frame, str) and ("/" in frame):
            segments = [s for s in frame.split("/") if s != ""]
        else:
            segments = [frame]
        for seg in segments:
            if isinstance(seg, int):
                d.switch_to.frame(seg)
            elif isinstance(seg, str) and seg.isdigit():
                d.switch_to.frame(int(seg))
            else:
                d.switch_to.frame(seg)

    def find(self, by, value, frame=None, timeout=10, interval=0.5):
        """Find an element, auto-waiting up to `timeout`s for it to appear (like
        Playwright). Re-applies the frame each poll so a mid-load reload recovers."""
        from selenium.common.exceptions import NoSuchElementException
        d = self.ensure()
        deadline = time.time() + max(0, timeout)
        while True:
            try:
                self._apply_frame(frame)
                els = d.find_elements(self._by(by), value)
                if els:
                    return els[0]
            except Exception:
                pass
            if time.time() >= deadline:
                raise NoSuchElementException(
                    f"no element {by}={value!r} (frame={frame}) within {timeout}s")
            time.sleep(interval)


SESSION = IeSession()


# --- tool implementations ----------------------------------------------------
def t_open(args):
    url = args.get("url") or DEFAULT_URL
    fresh = SESSION.driver is None
    # New session: open url AS PART OF creation (crosses PM boundary safely).
    d = SESSION.ensure(initial_url=url)
    if not fresh:
        # Existing session: a normal navigation is fine within the same zone.
        try:
            d.get(url)
        except Exception as exc:
            log("get() note:", exc)
    time.sleep(args.get("wait", 4))
    return text_result(f"opened {url}\ntitle: {d.title}\nurl: {d.current_url}")


def t_goto(args):
    url = args["url"]
    d = SESSION.ensure()
    try:
        d.get(url)
    except Exception as exc:
        log("get() note:", exc)
    time.sleep(args.get("wait", 3))
    return text_result(f"title: {d.title}\nurl: {d.current_url}")


def t_status(args):
    info = {
        "active": SESSION.driver is not None,
        "driver_pid": SESSION.driver_pid,
        "edge_pid": SESSION.edge_pid,
        "profile": SESSION.profile,
    }
    if SESSION.driver is None:
        info["alive"] = False
        return text_result(json.dumps(info, indent=2))
    try:
        d = SESSION.driver
        info["alive"] = True
        info["title"] = d.title
        info["url"] = d.current_url
        try:
            info["window_handles"] = len(d.window_handles)
        except Exception:
            pass
    except Exception as exc:
        info["alive"] = False
        info["error"] = str(exc).splitlines()[0]
    return text_result(json.dumps(info, indent=2))


def t_browsers(args):
    """List all Edge IE-mode windows + IEDriver processes, flagging the one we own."""
    rows = list_ie_browsers()
    for b in rows:
        b["mine"] = (b["kind"] == "edge-ie" and b["pid"] == SESSION.edge_pid) or \
                    (b["kind"] == "iedriver" and b["pid"] == SESSION.driver_pid)
    edges = [b for b in rows if b["kind"] == "edge-ie"]
    orphans = [b["pid"] for b in edges if not b["mine"]]
    return text_result(json.dumps({
        "current": {"driver_pid": SESSION.driver_pid, "edge_pid": SESSION.edge_pid,
                    "profile": SESSION.profile},
        "ie_browsers": edges,
        "iedriver_processes": [b["pid"] for b in rows if b["kind"] == "iedriver"],
        "orphan_edge_pids": orphans,
    }, indent=2))


def t_kill_orphans(args):
    """Kill IE-mode Edge windows / IEDriver processes NOT owned by the current session."""
    rows = list_ie_browsers()
    victims = []
    for b in rows:
        mine = (b["kind"] == "edge-ie" and b["pid"] == SESSION.edge_pid) or \
               (b["kind"] == "iedriver" and b["pid"] == SESSION.driver_pid)
        if not mine:
            victims.append(b["pid"])
    killed = kill_pids(victims)
    return text_result(json.dumps({"killed": killed, "kept_current": SESSION.edge_pid}, indent=2))


def t_frames(args):
    from selenium.webdriver.common.by import By
    d = SESSION.ensure()
    d.switch_to.default_content()
    n = len(d.find_elements(By.TAG_NAME, "frame") + d.find_elements(By.TAG_NAME, "iframe"))
    out = []
    # The legacy Trident engine throws on get_attribute()/Sizzle, so probe each
    # frame by switching into it (by index) and reading what survives.
    for i in range(n):
        d.switch_to.default_content()
        info = {"index": i}
        try:
            d.switch_to.frame(i)
            try:
                info["url"] = d.execute_script("return document.location.href")
            except Exception:
                info["url"] = None
            try:
                info["title"] = d.title
            except Exception:
                pass
            try:
                info["text_preview"] = d.find_element(By.TAG_NAME, "body").text[:120]
            except Exception:
                info["text_preview"] = None
        except Exception as exc:
            info["error"] = str(exc).splitlines()[0]
        out.append(info)
    d.switch_to.default_content()
    return text_result(json.dumps(out, indent=2))


def t_text(args):
    from selenium.webdriver.common.by import By
    d = SESSION.ensure()
    SESSION._apply_frame(args.get("frame"))
    txt = d.find_element(By.TAG_NAME, "body").text
    limit = args.get("limit", 8000)
    return text_result(txt[:limit])


def t_html(args):
    d = SESSION.ensure()
    SESSION._apply_frame(args.get("frame"))
    html = d.page_source
    limit = args.get("limit", 12000)
    return text_result(html[:limit])


def t_click(args):
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    action = args.get("action", "click")
    if action == "click":
        el.click()
    else:
        from selenium.webdriver.common.action_chains import ActionChains
        ac = ActionChains(SESSION.driver)
        if action == "double":
            ac.double_click(el).perform()
        elif action == "right":
            ac.context_click(el).perform()
        else:
            raise ValueError("action must be click|double|right")
    time.sleep(args.get("wait", 2))
    return text_result(f"{action}-clicked {args['by']}={args['value']}")


def t_upload(args):
    """Set a file <input type=file> by sending the local file path."""
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    el.send_keys(args["path"])
    return text_result(f"uploaded {args['path']} -> {args['by']}={args['value']}")


def t_dialog(args):
    """Handle a JS dialog (alert/confirm/prompt). action: accept|dismiss|text|sendkeys.

    Caveat: true OS-level IE modals can block IEDriver and may be unreachable here.
    """
    d = SESSION.ensure()
    action = args.get("action", "accept")
    try:
        al = d.switch_to.alert
    except Exception as exc:
        return text_result(json.dumps({"ok": False, "note": f"no dialog present: {str(exc).splitlines()[0]}"}))
    try:
        if action == "text":
            return text_result(json.dumps({"ok": True, "text": al.text}))
        if action == "sendkeys":
            al.send_keys(args.get("text", ""))
            al.accept()
            return text_result(json.dumps({"ok": True, "did": "sent+accepted"}))
        if action == "dismiss":
            txt = al.text
            al.dismiss()
            return text_result(json.dumps({"ok": True, "did": "dismissed", "text": txt}))
        txt = al.text
        al.accept()
        return text_result(json.dumps({"ok": True, "did": "accepted", "text": txt}))
    except Exception as exc:
        return text_result(json.dumps({"ok": False, "note": str(exc).splitlines()[0]}))


def t_fill(args):
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    if args.get("clear", True):
        el.clear()
    el.send_keys(args["text"])
    return text_result(f"filled {args['by']}={args['value']}")


def t_js(args):
    d = SESSION.ensure()
    SESSION._apply_frame(args.get("frame"))
    res = d.execute_script(args["script"])
    return text_result(json.dumps(res, default=str)[: args.get("limit", 8000)])


def t_screenshot(args):
    d = SESSION.ensure()
    if args.get("value"):
        # element-scoped screenshot
        el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
        log("screenshot: capturing element")
        png = el.screenshot_as_png
    else:
        try:
            d.switch_to.default_content()  # window screenshots from the top doc
        except Exception:
            pass
        log("screenshot: capturing")
        png = d.get_screenshot_as_png()
    log(f"screenshot: got {len(png)} bytes")
    if args.get("save_path"):
        with open(args["save_path"], "wb") as fh:
            fh.write(png)
        log(f"screenshot: saved {args['save_path']}")
    b64 = base64.b64encode(png).decode("ascii")
    if args.get("no_inline"):
        # avoid returning a giant base64 blob; just confirm + path
        return text_result(f"screenshot saved ({len(png)} bytes)"
                           + (f" -> {args['save_path']}" if args.get("save_path") else ""))
    return {"content": [{"type": "image", "data": b64, "mimeType": "image/png"}]}


def t_wait_text(args):
    """Poll until `contains` appears (or, with gone=True, disappears) in a frame.

    If no frame is given, all frames are scanned each round. With gone=False the
    success condition is the substring present in some frame; with gone=True it is
    the substring absent from every scanned frame (e.g. a spinner disappearing).
    """
    from selenium.webdriver.common.by import By
    d = SESSION.ensure()
    needle = args["contains"]
    ci = args.get("case_insensitive", True)
    if ci:
        needle = needle.lower()
    timeout = args.get("timeout", 45)
    interval = args.get("interval", 2)
    frame = args.get("frame")
    gone = args.get("gone", False)

    elapsed = 0
    last = ""
    while True:
        # A whole round can fail transiently while the app navigates (IEDriver
        # briefly reports "Unable to get browser"); treat that as "not yet".
        found_in = None
        round_ok = False
        try:
            d.switch_to.default_content()
            if frame is not None and frame != "":
                candidates = [frame]
            else:
                n = len(d.find_elements(By.TAG_NAME, "frame") + d.find_elements(By.TAG_NAME, "iframe"))
                candidates = list(range(n)) if n else [None]
            for cand in candidates:
                d.switch_to.default_content()
                try:
                    if cand is not None:
                        SESSION._apply_frame(cand)
                    txt = d.find_element(By.TAG_NAME, "body").text
                except Exception:
                    continue
                hay = txt.lower() if ci else txt
                if needle in hay:
                    found_in = cand
                    last = txt
                    break
                last = txt
            round_ok = True
        except Exception as exc:
            last = f"(transient: {str(exc).splitlines()[0]})"

        if round_ok:
            if not gone and found_in is not None:
                d.switch_to.default_content()
                return text_result(json.dumps({
                    "ok": True, "found": True, "frame": found_in,
                    "after_seconds": elapsed, "preview": last[:600],
                }, indent=2))
            if gone and found_in is None:
                d.switch_to.default_content()
                return text_result(json.dumps({
                    "ok": True, "gone": True, "after_seconds": elapsed,
                }, indent=2))
        if elapsed >= timeout:
            try:
                d.switch_to.default_content()
            except Exception:
                pass
            return text_result(json.dumps({
                "ok": False, "found": found_in is not None, "after_seconds": elapsed,
                "timed_out": True, "last_preview": last[:600],
            }, indent=2))
        time.sleep(interval)
        elapsed += interval


_SELECT_JS = r"""
var sel=arguments[0], mode=arguments[1], val=arguments[2];
var opts=sel.options, idx=-1;
for(var i=0;i<opts.length;i++){
  if(mode=='label' && opts[i].text==val){ idx=i; break; }
  if(mode=='value' && opts[i].value==val){ idx=i; break; }
  if(mode=='index' && i==parseInt(val,10)){ idx=i; break; }
}
if(idx<0) return 'NOTFOUND';
sel.selectedIndex=idx;
try{ if(sel.fireEvent){ sel.fireEvent('onchange'); }
     else { var e=document.createEvent('HTMLEvents'); e.initEvent('change',true,true); sel.dispatchEvent(e); } }catch(e){}
return 'OK:'+opts[idx].text;
"""


def t_select(args):
    """Select an <option> by label/value/index, set via JS + fire onchange.

    (Selenium's Select clicks the <option>, which the legacy Trident engine
    rejects with "Error executing JavaScript" — so we set it directly.)
    """
    d = SESSION.ensure()
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    if "label" in args:
        mode, val = "label", args["label"]
    elif "option_value" in args:
        mode, val = "value", args["option_value"]
    elif "index" in args:
        mode, val = "index", str(args["index"])
    else:
        raise ValueError("provide one of: label, option_value, index")
    res = d.execute_script(_SELECT_JS, el, mode, val)
    if res == "NOTFOUND":
        raise ValueError(f"option {mode}={val!r} not found in {args['by']}={args['value']}")
    time.sleep(args.get("wait", 2))
    return text_result(f"selected {mode}={val} -> {res[3:]}")


_KEY_MAP = None


def _key(name):
    global _KEY_MAP
    from selenium.webdriver.common.keys import Keys
    if _KEY_MAP is None:
        _KEY_MAP = {
            "enter": Keys.ENTER, "return": Keys.RETURN, "tab": Keys.TAB,
            "escape": Keys.ESCAPE, "esc": Keys.ESCAPE, "space": Keys.SPACE,
            "backspace": Keys.BACK_SPACE, "delete": Keys.DELETE, "del": Keys.DELETE,
            "up": Keys.ARROW_UP, "down": Keys.ARROW_DOWN, "left": Keys.ARROW_LEFT,
            "right": Keys.ARROW_RIGHT, "arrowup": Keys.ARROW_UP, "arrowdown": Keys.ARROW_DOWN,
            "arrowleft": Keys.ARROW_LEFT, "arrowright": Keys.ARROW_RIGHT,
            "home": Keys.HOME, "end": Keys.END, "pageup": Keys.PAGE_UP,
            "pagedown": Keys.PAGE_DOWN, "f5": Keys.F5,
        }
    return _KEY_MAP.get(name.lower(), name)


def t_press_key(args):
    """Press a key (Enter/Tab/Escape/arrows/etc. or a literal char) on an element or the page."""
    d = SESSION.ensure()
    key = _key(args["key"])
    if args.get("value"):
        el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
        el.send_keys(key)
        where = f"{args['by']}={args['value']}"
    else:
        SESSION._apply_frame(args.get("frame"))
        d.switch_to.active_element.send_keys(key)
        where = "active element"
    time.sleep(args.get("wait", 1))
    return text_result(f"pressed {args['key']} on {where}")


def t_back(args):
    d = SESSION.ensure()
    d.back()
    time.sleep(args.get("wait", 2))
    return text_result(f"back -> {d.current_url}")


def t_forward(args):
    d = SESSION.ensure()
    d.forward()
    time.sleep(args.get("wait", 2))
    return text_result(f"forward -> {d.current_url}")


def t_hover(args):
    from selenium.webdriver.common.action_chains import ActionChains
    d = SESSION.ensure()
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    ActionChains(d).move_to_element(el).perform()
    time.sleep(args.get("wait", 1))
    return text_result(f"hovered {args['by']}={args['value']}")


def t_resize(args):
    d = SESSION.ensure()
    d.set_window_size(int(args["width"]), int(args["height"]))
    return text_result(f"resized to {args['width']}x{args['height']}")


def t_get(args):
    """Read an element's text, value, or a named attribute via direct JS.

    (el.text / el.get_attribute use IEDriver's JS atoms, which fail on the legacy
    engine; arguments[0].innerText/value/getAttribute is simple enough to work.)
    """
    d = SESSION.ensure()
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    what = args.get("attr", "text")
    try:
        if what == "text":
            out = d.execute_script("return arguments[0].innerText;", el)
        elif what == "value":
            out = d.execute_script("return arguments[0].value;", el)
        else:
            out = d.execute_script("return arguments[0].getAttribute(arguments[1]);", el, what)
    except Exception:
        out = el.text if what == "text" else el.get_attribute(what)
    return text_result("" if out is None else str(out)[: args.get("limit", 4000)])


def t_wait_element(args):
    """Wait for an element (by/value) to appear — or, with gone=True, to disappear."""
    by = args["by"]
    value = args["value"]
    frame = args.get("frame")
    timeout = args.get("timeout", 20)
    interval = args.get("interval", 1)
    gone = args.get("gone", False)
    d = SESSION.ensure()
    elapsed = 0
    while True:
        present = False
        try:
            SESSION._apply_frame(frame)
            present = len(d.find_elements(SESSION._by(by), value)) > 0
        except Exception:
            present = False
        if (not gone and present) or (gone and not present):
            try:
                d.switch_to.default_content()
            except Exception:
                pass
            return text_result(json.dumps({
                "ok": True, "present": present, "after_seconds": elapsed,
            }))
        if elapsed >= timeout:
            return text_result(json.dumps({
                "ok": False, "present": present, "after_seconds": elapsed, "timed_out": True,
            }))
        time.sleep(interval)
        elapsed += interval


def t_scroll(args):
    """Scroll: bring an element into view (by/value), or scroll the page/a container.

    Modes:
      - by/value given      -> element.scrollIntoView()
      - to = top|bottom     -> scroll window (or container by/value) to that edge
      - dx/dy given         -> window.scrollBy(dx, dy)
    """
    d = SESSION.ensure()
    frame = args.get("frame")
    SESSION._apply_frame(frame)
    to = args.get("to")
    if args.get("value") and not to:
        el = SESSION.find(args["by"], args["value"], frame, args.get("timeout", 10))
        d.execute_script("arguments[0].scrollIntoView(true);", el)
        return text_result(f"scrolled into view: {args['by']}={args['value']}")
    if to in ("top", "bottom"):
        if args.get("value"):
            el = SESSION.find(args["by"], args["value"], frame, args.get("timeout", 10))
            pos = "0" if to == "top" else "arguments[0].scrollHeight"
            d.execute_script(f"arguments[0].scrollTop = {pos};", el)
            return text_result(f"scrolled container to {to}")
        y = "0" if to == "top" else "document.body.scrollHeight"
        d.execute_script(f"window.scrollTo(0, {y});")
        return text_result(f"scrolled page to {to}")
    dx = int(args.get("dx", 0))
    dy = int(args.get("dy", 0))
    d.execute_script("window.scrollBy(arguments[0], arguments[1]);", dx, dy)
    return text_result(f"scrolled by ({dx},{dy})")


# Extract a tabular grid into structured rows. Returns a \x1e-row / \x1f-cell
# delimited string (no JSON — IE7 has no window.JSON). Picks the table with the
# most rows that have >=2 cells (skips single-cell wrapper/layout tables).
_GRID_JS = r"""
var sel = arguments[0];
function clean(s){ return (s||'').replace(/[\r\n\t]+/g,' ').replace(//g,' ').replace(//g,' ').replace(/^ +| +$/g,''); }
var table = null;
if(sel){ try{ table = document.getElementById(sel); }catch(e){} }
if(!table){
  var tabs = document.getElementsByTagName('table'), bestScore=-1;
  for(var i=0;i<tabs.length;i++){
    var rws = tabs[i].rows, multi=0, maxc=0;
    for(var r=0;r<rws.length;r++){ var n=rws[r].cells.length; if(n>=2) multi++; if(n>maxc) maxc=n; }
    var score = multi*1000 + maxc;
    if(multi>0 && score>bestScore){ bestScore=score; table=tabs[i]; }
  }
}
if(!table) return '';
var out=[];
var rws=table.rows;
for(var r=0;r<rws.length;r++){
  var cells=rws[r].cells; if(cells.length<2) continue;
  var row=[];
  for(var c=0;c<cells.length;c++){ row.push(clean(cells[c].innerText)); }
  out.push(row.join(''));
}
return out.join('');
"""


def t_grid(args):
    """Extract a tabular grid as structured rows (header-keyed dicts when possible).

    Heuristic + app-agnostic: picks the table with the most multi-cell rows. Pass a
    table element id via `table_id` to target a specific grid. Returns headers + rows;
    if `headers` is given (or detected) and matches the column count, rows become dicts.
    """
    d = SESSION.ensure()
    SESSION._apply_frame(args.get("frame"))
    raw = d.execute_script(_GRID_JS, args.get("table_id"))
    if not raw:
        return text_result(json.dumps({"rows": 0, "note": "no table found"}))
    grid = [r.split("\x1f") for r in raw.split("\x1e") if r != ""]
    grid = [r for r in grid if any(c.strip() for c in r)]  # drop spacer/empty rows
    headers = args.get("headers")
    if not headers and grid:
        # detect header row: first row whose cells are all non-empty & non-numeric-ish
        first = grid[0]
        if first and all(c.strip() and not c.strip().isdigit() for c in first):
            headers = first
            grid = grid[1:]
    limit = args.get("limit", 500)
    grid = grid[:limit]
    if headers and all(len(r) == len(headers) for r in grid):
        rows = [dict(zip(headers, r)) for r in grid]
    else:
        rows = grid  # fall back to arrays
    return text_result(json.dumps(
        {"columns": len(headers) if headers else (len(grid[0]) if grid else 0),
         "headers": headers, "row_count": len(rows), "rows": rows},
        ensure_ascii=False, indent=2)[: args.get("out_limit", 12000)])


def t_sleep(args):
    secs = float(args.get("seconds", 1))
    time.sleep(secs)
    return text_result(f"slept {secs}s")


# App-agnostic readiness probe. Run from the top document; it recurses into every
# same-origin frame and reports (a) whether every document.readyState is 'complete'
# and (b) content-size metrics used to detect when the DOM has stopped changing.
# Cross-origin frames are skipped (counted, not read). No app-specific assumptions.
# NOTE: returns a \x1f-delimited string, NOT JSON — pages in IE7/quirks document
# mode have no window.JSON, so JSON.stringify() throws "'JSON' is undefined".
_READY_PROBE_JS = r"""
function walk(w, acc){
  try{
    var d = w.document;
    acc.docs++;
    if(d.readyState !== 'complete') acc.ready = false;
    var bt = '';
    try{ bt = d.body ? d.body.innerText : ''; }catch(e){ bt = ''; }
    acc.len += bt.length;
    try{ acc.els += d.getElementsByTagName('*').length; }catch(e){}
    if(bt.length > acc.maxLen){ acc.maxLen = bt.length; acc.sample = bt.substring(0,300); }
  }catch(e){ acc.crossOrigin++; }
  try{ for(var i=0;i<w.frames.length;i++) walk(w.frames[i], acc); }catch(e){}
  return acc;
}
var a = walk(window, {docs:0, ready:true, len:0, els:0, crossOrigin:0, maxLen:0, sample:''});
return [a.ready, a.len, a.els, a.docs, a.crossOrigin, a.sample].join('');
"""


def _parse_ready(raw):
    """Parse the \x1f-delimited readiness string into a dict."""
    parts = (raw or "").split("\x1f")
    parts += [""] * (6 - len(parts))
    to_int = lambda v: int(v) if str(v).isdigit() else 0
    return {
        "ready": parts[0] == "true",
        "len": to_int(parts[1]),
        "els": to_int(parts[2]),
        "docs": to_int(parts[3]),
        "crossOrigin": to_int(parts[4]),
        "sample": parts[5],
    }


def t_wait_ready(args):
    """Wait until the page (and all same-origin frames) finish loading — app-agnostic.

    "Loaded" = every document.readyState is 'complete' AND the DOM has stopped
    changing for `stable_rounds` consecutive polls (content settled), with at least
    `min_chars` of visible text. This handles slow async/AJAX/frameset content
    (e.g. ~30s grids) without knowing anything about the specific application.
    """
    d = SESSION.ensure()
    timeout = args.get("timeout", 60)
    interval = args.get("interval", 2)
    stable_rounds = args.get("stable_rounds", 2)
    min_chars = args.get("min_chars", 1)
    require_settle = args.get("settle", True)

    elapsed = 0
    prev_sig = None
    stable = 0
    last = {"loaded": False, "stage": "init"}
    while True:
        try:
            d.switch_to.default_content()
            raw = d.execute_script(_READY_PROBE_JS)
            s = _parse_ready(raw)
            sig = (s.get("len", 0), s.get("els", 0), s.get("docs", 0))
            ready = bool(s.get("ready")) and s.get("len", 0) >= min_chars
            if prev_sig is not None and sig == prev_sig:
                stable += 1
            else:
                stable = 0
            prev_sig = sig
            settled = (not require_settle) or (stable >= stable_rounds)
            last = {
                "loaded": bool(ready and settled),
                "stage": "ready" if (ready and settled) else ("settling" if ready else "loading"),
                "readyState_all_complete": bool(s.get("ready")),
                "frames": s.get("docs", 0),
                "cross_origin_frames": s.get("crossOrigin", 0),
                "text_len": s.get("len", 0),
                "elements": s.get("els", 0),
                "stable_rounds": stable,
                "sample": s.get("sample", ""),
                "after_seconds": elapsed,
            }
            if last["loaded"]:
                return text_result(json.dumps(last, indent=2))
        except Exception as exc:
            stable = 0
            last = {"loaded": False, "stage": "transient",
                    "note": str(exc).splitlines()[0], "after_seconds": elapsed}
        if elapsed >= timeout:
            last["timed_out"] = True
            return text_result(json.dumps(last, indent=2))
        time.sleep(interval)
        elapsed += interval


def t_close(args):
    SESSION.quit()
    return text_result("session closed")


def text_result(s):
    return {"content": [{"type": "text", "text": s}]}


TOOLS = [
    {
        "name": "ie_open",
        "description": "Start (or reuse) an Edge IE-mode session and navigate to a URL "
                       "(default IE_DEFAULT_URL). Keeps the session alive for later calls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": f"URL to open (default {DEFAULT_URL})"},
                "wait": {"type": "number", "description": "seconds to wait after load (default 4)"},
            },
        },
        "fn": t_open,
    },
    {
        "name": "ie_goto",
        "description": "Navigate the current IE-mode session to a new URL.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "wait": {"type": "number"}},
            "required": ["url"],
        },
        "fn": t_goto,
    },
    {
        "name": "ie_status",
        "description": "Session state as JSON: active/alive, title, url, window_handles, and the "
                       "browser this session owns (driver_pid, edge_pid, profile).",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_status,
    },
    {
        "name": "ie_browsers",
        "description": "List ALL Edge IE-mode windows and IEDriver processes open on the machine, "
                       "flagging which one THIS session owns (mine=true) and which are orphans. "
                       "Use to see leftover browsers from prior/other runs.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_browsers,
    },
    {
        "name": "ie_kill_orphans",
        "description": "Terminate IE-mode Edge windows / IEDriver processes NOT owned by the current "
                       "session (cleanup of leftovers). Keeps the current session's browser.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_kill_orphans,
    },
    {
        "name": "ie_frames",
        "description": "List the frames/iframes of the current page (index, name, id, src). "
                       "Use the index or name with the 'frame' arg of other tools.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_frames,
    },
    {
        "name": "ie_text",
        "description": "Get visible text of the page or of a specific frame.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "frame": {"type": ["string", "integer"], "description": "frame index or name (optional)"},
                "limit": {"type": "integer"},
            },
        },
        "fn": t_text,
    },
    {
        "name": "ie_html",
        "description": "Get the HTML source of the page or of a specific frame.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "frame": {"type": ["string", "integer"]},
                "limit": {"type": "integer"},
            },
        },
        "fn": t_html,
    },
    {
        "name": "ie_click",
        "description": "Click an element (auto-waits). 'by' is id/css/xpath/name/link_text/tag/class. "
                       "action: click (default) | double | right.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "value": {"type": "string"},
                "action": {"type": "string", "description": "click | double | right"},
                "frame": {"type": ["string", "integer"]},
                "timeout": {"type": "number"},
                "wait": {"type": "number"},
            },
            "required": ["by", "value"],
        },
        "fn": t_click,
    },
    {
        "name": "ie_fill",
        "description": "Type text into an input/textarea element (auto-waits).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "value": {"type": "string"},
                "text": {"type": "string"},
                "frame": {"type": ["string", "integer"]},
                "clear": {"type": "boolean"},
            },
            "required": ["by", "value", "text"],
        },
        "fn": t_fill,
    },
    {
        "name": "ie_upload",
        "description": "Set a file <input type=file> by sending a local file path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "value": {"type": "string"},
                "path": {"type": "string", "description": "absolute local file path"},
                "frame": {"type": ["string", "integer"]},
            },
            "required": ["by", "value", "path"],
        },
        "fn": t_upload,
    },
    {
        "name": "ie_dialog",
        "description": "Handle a JS dialog (alert/confirm/prompt). action: accept (default) | dismiss "
                       "| text | sendkeys (with text). Note: OS-level IE modals may be unreachable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "accept | dismiss | text | sendkeys"},
                "text": {"type": "string", "description": "text for sendkeys"},
            },
        },
        "fn": t_dialog,
    },
    {
        "name": "ie_js",
        "description": "Execute JavaScript in the page or a frame and return the result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "frame": {"type": ["string", "integer"]},
                "limit": {"type": "integer"},
            },
            "required": ["script"],
        },
        "fn": t_js,
    },
    {
        "name": "ie_screenshot",
        "description": "Capture a PNG screenshot of the IE-mode window, or of a single element if "
                       "by/value are given (returned as an image; optionally saved to save_path).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "save_path": {"type": "string"},
                "no_inline": {"type": "boolean", "description": "return confirmation text instead of the image blob"},
                "by": {"type": "string", "description": "element locator type (for element screenshot)"},
                "value": {"type": "string", "description": "element locator value (for element screenshot)"},
                "frame": {"type": ["string", "integer"]},
            },
        },
        "fn": t_screenshot,
    },
    {
        "name": "ie_wait_text",
        "description": "Poll until a specific substring appears in the page text (for slow async "
                       "content). Scans all frames unless 'frame' is given; returns which frame "
                       "matched + a preview. For 'is the page loaded?' use ie_wait_ready instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contains": {"type": "string"},
                "frame": {"type": ["string", "integer"]},
                "timeout": {"type": "number", "description": "max seconds (default 45)"},
                "interval": {"type": "number", "description": "poll seconds (default 2)"},
                "case_insensitive": {"type": "boolean"},
                "gone": {"type": "boolean", "description": "wait for the substring to DISAPPEAR instead of appear (e.g. a spinner)"},
            },
            "required": ["contains"],
        },
        "fn": t_wait_text,
    },
    {
        "name": "ie_wait_ready",
        "description": "Wait until the page finishes loading: every same-origin frame's "
                       "document.readyState is 'complete' AND the DOM stops changing for a few "
                       "polls (content settled). Handles slow async/frameset content. Use THIS to "
                       "know content is ready, then search the data separately. Do NOT wait for a "
                       "specific value to detect load — if it's absent you can't tell 'still "
                       "loading' from 'not present'. Returns loaded=true/false with frame count, "
                       "text length, element count and a content sample.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout": {"type": "number", "description": "max seconds (default 60)"},
                "interval": {"type": "number", "description": "poll seconds (default 2)"},
                "stable_rounds": {"type": "integer", "description": "consecutive unchanged polls to call it settled (default 2)"},
                "min_chars": {"type": "integer", "description": "minimum visible text length to count as loaded (default 1)"},
                "settle": {"type": "boolean", "description": "require DOM to stop changing (default true); set false to only require readyState complete"},
            },
        },
        "fn": t_wait_ready,
    },
    {
        "name": "ie_select",
        "description": "Select an <option> in a <select> dropdown, by label (visible text), "
                       "option_value, or index. Provide exactly one of label/option_value/index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "value": {"type": "string"},
                "label": {"type": "string", "description": "option visible text"},
                "option_value": {"type": "string", "description": "option value attribute"},
                "index": {"type": "integer", "description": "option index (0-based)"},
                "frame": {"type": ["string", "integer"]},
            },
            "required": ["by", "value"],
        },
        "fn": t_select,
    },
    {
        "name": "ie_press_key",
        "description": "Press a key on an element (if by/value given) or the active element. "
                       "Key names: Enter, Tab, Escape, Space, Backspace, Delete, Up/Down/Left/Right, "
                       "Home, End, PageUp, PageDown, F5 — or a literal character.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "by": {"type": "string"},
                "value": {"type": "string"},
                "frame": {"type": ["string", "integer"]},
                "wait": {"type": "number"},
            },
            "required": ["key"],
        },
        "fn": t_press_key,
    },
    {
        "name": "ie_back",
        "description": "Navigate back in browser history.",
        "inputSchema": {"type": "object", "properties": {"wait": {"type": "number"}}},
        "fn": t_back,
    },
    {
        "name": "ie_forward",
        "description": "Navigate forward in browser history.",
        "inputSchema": {"type": "object", "properties": {"wait": {"type": "number"}}},
        "fn": t_forward,
    },
    {
        "name": "ie_hover",
        "description": "Hover the mouse over an element (for menus/tooltips).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "value": {"type": "string"},
                "frame": {"type": ["string", "integer"]},
                "wait": {"type": "number"},
            },
            "required": ["by", "value"],
        },
        "fn": t_hover,
    },
    {
        "name": "ie_resize",
        "description": "Resize the browser window (useful for consistent screenshots).",
        "inputSchema": {
            "type": "object",
            "properties": {"width": {"type": "integer"}, "height": {"type": "integer"}},
            "required": ["width", "height"],
        },
        "fn": t_resize,
    },
    {
        "name": "ie_get",
        "description": "Read an element's text (default), its 'value', or a named attribute/property. "
                       "Set attr=text|value|<attribute name>.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "value": {"type": "string"},
                "attr": {"type": "string", "description": "text (default) | value | any attribute name"},
                "frame": {"type": ["string", "integer"]},
                "limit": {"type": "integer"},
            },
            "required": ["by", "value"],
        },
        "fn": t_get,
    },
    {
        "name": "ie_wait_element",
        "description": "Wait for an element (by/value) to appear — or, with gone=true, to disappear. "
                       "Use before acting on slow-rendering content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "value": {"type": "string"},
                "frame": {"type": ["string", "integer"]},
                "timeout": {"type": "number", "description": "max seconds (default 20)"},
                "interval": {"type": "number"},
                "gone": {"type": "boolean", "description": "wait for it to disappear instead"},
            },
            "required": ["by", "value"],
        },
        "fn": t_wait_element,
    },
    {
        "name": "ie_scroll",
        "description": "Scroll: bring an element into view (by/value), scroll page/container to "
                       "top|bottom (to=...), or by pixels (dx/dy). Needed for virtualized grids.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string"},
                "value": {"type": "string"},
                "to": {"type": "string", "description": "top | bottom"},
                "dx": {"type": "integer"},
                "dy": {"type": "integer"},
                "frame": {"type": ["string", "integer"]},
            },
        },
        "fn": t_scroll,
    },
    {
        "name": "ie_grid",
        "description": "Extract a tabular grid as structured rows. Auto-picks the table with the "
                       "most multi-cell rows (or pass table_id). Returns headers + rows (header-keyed "
                       "dicts when the header row is detected/given). App-agnostic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "frame": {"type": ["string", "integer"], "description": "frame/path holding the grid (e.g. '3/0')"},
                "table_id": {"type": "string", "description": "specific table element id (optional)"},
                "headers": {"type": "array", "items": {"type": "string"}, "description": "explicit column names (optional)"},
                "limit": {"type": "integer", "description": "max rows (default 500)"},
                "out_limit": {"type": "integer", "description": "max output chars (default 12000)"},
            },
        },
        "fn": t_grid,
    },
    {
        "name": "ie_sleep",
        "description": "Sleep for N seconds (last resort; prefer ie_wait_ready / ie_wait_text).",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
        },
        "fn": t_sleep,
    },
    {
        "name": "ie_close",
        "description": "Close the IE-mode session and quit the browser.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_close,
    },
]
TOOL_MAP = {t["name"]: t for t in TOOLS}


# --- MCP stdio JSON-RPC loop -------------------------------------------------
def public_tools():
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in TOOLS]


def handle(msg):
    """Return a response dict, or None for notifications."""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        client_ver = params.get("protocolVersion", PROTOCOL_VERSION)
        return ok(mid, {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return ok(mid, {})
    if method == "tools/list":
        return ok(mid, {"tools": public_tools()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOL_MAP.get(name)
        if tool is None:
            return err(mid, -32602, f"unknown tool: {name}")
        try:
            result = tool["fn"](args)
            return ok(mid, result)
        except Exception as exc:
            log("tool error:", traceback.format_exc())
            return ok(mid, {
                "content": [{"type": "text", "text": f"ERROR in {name}: {exc}"}],
                "isError": True,
            })
    if mid is not None:
        return err(mid, -32601, f"method not found: {method}")
    return None


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def selftest():
    """Diagnose the setup without an MCP client: checks deps/paths, then opens and
    closes a real IE-mode session against IE_DEFAULT_URL. Prints a report; exit 0/1."""
    ok = True
    print("ie-mcp selftest")
    try:
        import selenium
        print(f"  selenium: {selenium.__version__}")
    except Exception as exc:
        print(f"  selenium: FAIL ({exc})"); ok = False
    for label, path in [("edge", EDGE), ("iedriver", IE_DRIVER)]:
        exists = os.path.exists(path)
        print(f"  {label}: {'OK' if exists else 'MISSING'} {path}")
        ok = ok and exists
    print(f"  site_list: {SITE_LIST or '(none)'}")
    print(f"  default_url: {DEFAULT_URL}")
    print("  opening a session ...")
    try:
        ensure_ie_policies()
        d = SESSION.ensure(initial_url=DEFAULT_URL)
        time.sleep(3)
        print(f"  session OK: title={d.title!r} url={d.current_url}")
        ua = d.execute_script("return navigator.userAgent;")
        is_ie = ("Trident" in ua or "MSIE" in ua) and "Chrome/" not in ua
        print(f"  userAgent: {ua}")
        print(f"  IE engine: {'YES' if is_ie else 'NO (Chromium?)'}")
        ok = ok and is_ie
    except Exception as exc:
        print(f"  session: FAIL ({exc})"); ok = False
    finally:
        SESSION.quit()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass
    ensure_ie_policies()
    log("ready")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log(f"DROP non-JSON line ({len(line)} chars)")
                continue
            log(f"recv id={msg.get('id')} method={msg.get('method')} "
                f"tool={(msg.get('params') or {}).get('name')}")
            resp = handle(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
    finally:
        SESSION.quit()
        release_lock()


if __name__ == "__main__":
    main()
