"""One-shot headless-Chrome smoke test for the chat tools popover.

Spawns Chrome with --headless --remote-debugging-port=9222, mints an
admin session cookie via AUTH_SECRET_KEY (no password needed), drives
the page through Chrome DevTools Protocol, clicks the 🔧 button, and
reports:

  - any JS console errors
  - the rendered #tools-list HTML (truncated)
  - tier/group/subgroup counts in the live DOM
  - a screenshot of the popover (PNG written to data/eval/)

Exits 0 on success (taxonomy populated, no errors), 1 on any failure.

Used after the c930503 + 679ab41 fixes to confirm the popover actually
renders on a real Chromium engine — no curl-only verification this time.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets


REPO_ROOT = Path(__file__).resolve().parent.parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEBUG_PORT = 9222
# Loopback the static asset directly: the host_gate middleware refuses
# "/" from non-chat-subdomain origins (it returns "Chat is only reachable
# at https://chat.mylensandi.com." for loopback), but /static/* is in
# _ALWAYS_ALLOWED_PREFIXES. FastAPI's StaticFiles re-reads the file per
# request, so this path serves whatever chat.html is currently on disk —
# i.e. the latest self-heal build, regardless of whether the backend has
# been bounced to pick up the new "/" redirect handler.
URL = "http://127.0.0.1:18000/static/chat.html"
EVAL_DIR = REPO_ROOT / "data" / "eval"
EVAL_DIR.mkdir(parents=True, exist_ok=True)


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def mint_session_token(user_id: int = 1, days: int = 30) -> str:
    _load_dotenv()
    from jose import jwt   # type: ignore
    key = os.environ["AUTH_SECRET_KEY"]
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + days * 86400},
        key, algorithm="HS256",
    )


class CDP:
    """Single-reader CDP client. All websocket reads happen in one task;
    request/response futures get resolved by id, events go to lists."""

    def __init__(self, ws):
        self.ws = ws
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.console: list[dict] = []
        self.exceptions: list[dict] = []
        self._reader = asyncio.create_task(self._read())

    def _id(self) -> int:
        self._next += 1
        return self._next

    async def _read(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                    continue
                method = msg.get("method", "")
                if method == "Runtime.consoleAPICalled":
                    p = msg.get("params", {})
                    text = " ".join(
                        (a.get("value") or a.get("description") or str(a))
                        for a in p.get("args", [])
                    )
                    self.console.append({"level": p.get("type"), "text": text})
                elif method == "Runtime.exceptionThrown":
                    self.exceptions.append(
                        msg.get("params", {}).get("exceptionDetails", {})
                    )
        except Exception:
            pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("websocket closed"))

    async def send(self, method: str, params: dict | None = None) -> dict:
        mid = self._id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({
            "id": mid, "method": method, "params": params or {},
        }))
        return await fut

    async def close(self):
        self._reader.cancel()
        try:
            await self._reader
        except (asyncio.CancelledError, Exception):
            pass


async def drive():
    token = mint_session_token()
    user_dir = Path(tempfile.mkdtemp(prefix="chat-headless-"))
    chrome_args = [
        CHROME,
        "--headless=new",
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={user_dir}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-extensions",
        "--window-size=1280,900",
        "about:blank",
    ]
    proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    print(f"[chrome] spawned pid={proc.pid}")

    # Poll for the debug endpoint.
    target_ws = None
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2,
            ) as r:
                json.load(r)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=2,
            ) as r:
                tabs = json.load(r)
            for t in tabs:
                if t.get("type") == "page":
                    target_ws = t["webSocketDebuggerUrl"]
                    break
            if target_ws:
                break
        except Exception:
            time.sleep(0.4)
    if not target_ws:
        proc.kill()
        raise RuntimeError("Chrome debug port never came up")
    print(f"[cdp] connected to {target_ws}")

    fail = False
    try:
        async with websockets.connect(
            target_ws, max_size=20 * 1024 * 1024,
        ) as ws:
            cdp = CDP(ws)

            # Enable runtime + page + network so we capture console + load events.
            await cdp.send("Runtime.enable")
            await cdp.send("Page.enable")
            await cdp.send("Network.enable")

            # Set the auth cookie BEFORE navigating.
            await cdp.send("Network.setCookie", {
                "name": "lai_session", "value": token,
                "url": "http://127.0.0.1:18000/", "path": "/",
                "httpOnly": True, "sameSite": "Lax",
            })

            # Navigate.
            await cdp.send("Page.navigate", {"url": URL})
            # Wait for load + bootstrap async tasks (loadModels, loadTools,
            # loadConversations all kick off).
            await asyncio.sleep(4)

            # Click the 🔧 button to open the popover.
            click_js = """
              (() => {
                const b = document.getElementById('tools-btn');
                if (!b) return {clicked: false, reason: 'no tools-btn'};
                b.click();
                return {clicked: true};
              })()
            """
            await cdp.send("Runtime.evaluate", {
                "expression": click_js, "returnByValue": True,
            })
            await asyncio.sleep(1.5)   # let any lazy load + render settle

            # Surgical trace inside renderTools — replicate its body
            # step-by-step to find where iteration drops out without
            # writing.
            rerun_js = """
              (() => {
                const log = [];
                const list = document.getElementById('tools-list');
                const search = document.getElementById('tool-search');
                log.push('toolsList el: ' + (list ? 'yes' : 'NO'));
                log.push('toolSearch el: ' + (search ? 'yes' : 'NO'));
                log.push('toolSearch.value: ' + JSON.stringify(search ? search.value : '(no el)'));
                log.push('_toolsLoadState: ' + JSON.stringify(_toolsLoadState));
                log.push('toolsTaxonomy.length: ' + toolsTaxonomy.length);
                log.push('toolState.size: ' + toolState.size);
                if (toolsTaxonomy.length) {
                  const t0 = toolsTaxonomy[0];
                  log.push('  tier[0].keys: ' + JSON.stringify(Object.keys(t0)));
                  log.push('  tier[0].groups.length: ' + (t0.groups ? t0.groups.length : 'no groups'));
                  if (t0.groups && t0.groups.length) {
                    const g0 = t0.groups[0];
                    log.push('    group[0].keys: ' + JSON.stringify(Object.keys(g0)));
                    log.push('    group[0].subgroups.length: ' + (g0.subgroups ? g0.subgroups.length : 'no subs'));
                    if (g0.subgroups && g0.subgroups.length) {
                      const s0 = g0.subgroups[0];
                      log.push('      sub[0].keys: ' + JSON.stringify(Object.keys(s0)));
                      log.push('      sub[0].tools.length: ' + (s0.tools ? s0.tools.length : 'no tools'));
                    }
                  }
                  // Replicate the first line of the tier loop directly.
                  try {
                    const tierTools = t0.groups.flatMap(g => g.subgroups.flatMap(s => s.tools));
                    log.push('  computed tierTools.length for tier[0]: ' + tierTools.length);
                    log.push('  nodeMatches(tierTools, ""): ' + nodeMatches(tierTools, ''));
                  } catch (e) {
                    log.push('  flatMap THREW: ' + e);
                  }
                }
                // CRITICAL: is the closure's `toolsList` ref still the same element
                // as the live DOM's #tools-list? If the popover was ever
                // re-rendered via innerHTML assignment on its parent, the
                // closure ref points at an orphaned/detached node.
                log.push('toolsList === live #tools-list: ' + (toolsList === document.getElementById('tools-list')));
                log.push('toolsList isConnected: ' + toolsList.isConnected);
                log.push('toolsList parent: ' + (toolsList.parentNode ? toolsList.parentNode.id || toolsList.parentNode.tagName : 'NONE'));
                log.push('toolsPop === live #tools-popover: ' + (toolsPop === document.getElementById('tools-popover')));
                // Now actually call renderTools and snapshot the list before/after.
                let renderErr = null;
                const before = list.innerHTML;
                try { renderTools(); } catch (e) { renderErr = String(e) + ' :: ' + (e.stack || ''); }
                const after = list.innerHTML;
                log.push('renderTools err: ' + JSON.stringify(renderErr));
                log.push('toolsList.innerHTML len before: ' + before.length + ', after: ' + after.length);
                log.push('after head: ' + JSON.stringify(after.slice(0, 400)));
                return log;
              })()
            """
            r = await cdp.send("Runtime.evaluate", {
                "expression": rerun_js, "returnByValue": True,
            })
            print("=== MANUAL renderTools() ===")
            print(json.dumps(r.get("result", {}).get("result", {}).get("value", {}), indent=2))
            print()

            # Diagnostic: read the let-scoped popover state via eval (top-
            # level let/const bindings live in script scope, NOT on window).
            probe_js = """
              (() => {
                const out = {};
                try { out.toolsTaxonomy_len = toolsTaxonomy.length; } catch(e) { out.toolsTaxonomy_err = String(e); }
                try { out.toolState_size = toolState.size; } catch(e) { out.toolState_err = String(e); }
                try { out._toolsLoadState = _toolsLoadState; } catch(e) { out._toolsLoadState_err = String(e); }
                try { out._toolsLoadErr = _toolsLoadErr; } catch(e) {}
                try { out.loadTools_type = typeof loadTools; } catch(e) { out.loadTools_err = String(e); }
                try { out.renderTools_type = typeof renderTools; } catch(e) { out.renderTools_err = String(e); }
                try { out.renderTools_src_head = renderTools.toString().slice(0, 200); } catch(e) {}
                try { out.loadTools_src_head = loadTools.toString().slice(0, 240); } catch(e) {}
                return out;
              })()
            """
            r = await cdp.send("Runtime.evaluate", {
                "expression": probe_js, "returnByValue": True,
            })
            state = r.get("result", {}).get("result", {}).get("value", {})
            print("=== POPOVER STATE ===")
            print(json.dumps(state, indent=2))
            print()

            # Diagnostic: hit /admin/me directly via fetch to see why
            # bootstrap() isn't getting past the auth check.
            diag_js = """
              (async () => {
                const out = {};
                try {
                  const r = await fetch('/admin/me', {credentials: 'include'});
                  out.admin_me_status = r.status;
                  out.admin_me_body = (await r.text()).slice(0, 200);
                } catch (e) { out.admin_me_err = String(e); }
                try {
                  const r2 = await fetch('/me', {credentials: 'include'});
                  out.me_status = r2.status;
                } catch (e) { out.me_err = String(e); }
                out.cookies_visible_to_js = document.cookie;   // httpOnly hides lai_session
                out.location = location.href;
                return out;
              })()
            """
            r = await cdp.send("Runtime.evaluate", {
                "expression": diag_js, "returnByValue": True, "awaitPromise": True,
            })
            diag = r.get("result", {}).get("result", {}).get("value", {})
            print("=== DIAG ===")
            print(json.dumps(diag, indent=2))
            print()

            # Also list the cookies the browser actually has for this origin.
            cks = await cdp.send("Network.getCookies", {
                "urls": ["http://127.0.0.1:18000/"],
            })
            ck_list = cks.get("result", {}).get("cookies", [])
            print(f"=== COOKIES ({len(ck_list)}) ===")
            for c in ck_list:
                print(f"  {c.get('name')}={(c.get('value') or '')[:30]}... "
                      f"path={c.get('path')} httpOnly={c.get('httpOnly')} "
                      f"secure={c.get('secure')} sameSite={c.get('sameSite')}")
            print()

            # Inspect the rendered popover. Note: #chatapp not #app, and
            # loadTools/_toolsLoadState are let-scoped (not on window) — so
            # we probe the served HTML for the self-heal markers and the
            # DOM for the rendered state.
            inspect_js = """
              (() => {
                const pop = document.getElementById('tools-popover');
                const list = document.getElementById('tools-list');
                if (!pop || !list) return {error: 'popover or list not in DOM'};
                const html = document.documentElement.outerHTML;
                return {
                  pop_hidden: pop.classList.contains('hidden'),
                  list_html_len: (list.innerHTML || '').length,
                  list_html: (list.innerHTML || '').slice(0, 800),
                  tier_blocks: list.querySelectorAll('.tier-block').length,
                  tier_headers: list.querySelectorAll('.tier-header').length,
                  group_headers: list.querySelectorAll('.group-header').length,
                  subgroup_headers: list.querySelectorAll('.subgroup-header').length,
                  tool_rows: list.querySelectorAll('.tool-row').length,
                  retry_btn: !!list.querySelector('#tools-retry'),
                  // Are we actually on the chat UI (chatapp visible) or
                  // still at the signin form?
                  on_signin: !document.getElementById('chatapp')
                    || document.getElementById('chatapp').classList.contains('hidden'),
                  signin_visible: !document.getElementById('signin').classList.contains('hidden'),
                  // Is the current chat.html the new self-heal build?
                  has_self_heal: html.indexOf('_toolsLoadState') !== -1,
                  has_retry_marker: html.indexOf('tools-retry') !== -1,
                  // Re-fetch /tools to confirm response shape from THIS browser context.
                };
              })()
            """
            r = await cdp.send("Runtime.evaluate", {
                "expression": inspect_js, "returnByValue": True,
            })
            inspection = r.get("result", {}).get("result", {}).get("value", {})

            # Take a screenshot.
            shot = await cdp.send("Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": False,
            })
            png_b64 = shot.get("result", {}).get("data")
            png_path = None
            if png_b64:
                png_path = EVAL_DIR / f"popover-headless-{int(time.time())}.png"
                png_path.write_bytes(base64.b64decode(png_b64))

            console_msgs = list(cdp.console)
            page_errors = list(cdp.exceptions)
            await cdp.close()

        # ── Report ──
        print()
        print("=== INSPECTION ===")
        print(json.dumps(inspection, indent=2))
        print()
        print(f"=== CONSOLE MESSAGES ({len(console_msgs)}) ===")
        for m in console_msgs:
            print(f"  [{m['level']}] {m['text'][:200]}")
        print()
        print(f"=== PAGE EXCEPTIONS ({len(page_errors)}) ===")
        for e in page_errors:
            print(f"  {e.get('text')}")
            if e.get("exception"):
                print(f"    → {e['exception'].get('description', '')[:300]}")
        print()
        if png_path:
            print(f"Screenshot: {png_path}")

        # ── Verdict ──
        if inspection.get("in_signin"):
            print("\nFAIL: still on sign-in screen — cookie didn't auth")
            fail = True
        elif inspection.get("pop_hidden"):
            print("\nFAIL: popover is still .hidden after click")
            fail = True
        elif inspection.get("retry_btn"):
            print("\nFAIL: popover rendered the error/Retry state")
            fail = True
        elif inspection.get("tier_blocks", 0) < 1:
            print(f"\nFAIL: popover has 0 tier blocks; load_state={inspection.get('load_state')}")
            fail = True
        elif inspection.get("tool_rows", 0) < 100:
            print(f"\nFAIL: only {inspection.get('tool_rows')} tool rows rendered (expected hundreds)")
            fail = True
        else:
            print("\nPASS: popover rendered with tier/group/subgroup tree")

        if page_errors:
            print(f"  (also {len(page_errors)} page-level exceptions — see above)")
            fail = True

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        try:
            shutil.rmtree(user_dir, ignore_errors=True)
        except Exception:
            pass

    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(drive())
