"""Screenshots for USER_GUIDE.md (guide-images/), clipped to the part of each page shown.

Render the sample pages first, then shoot them:

    .venv/bin/python scripts/render_previews.py /tmp/previews
    .venv/bin/python scripts/guide_screenshots.py /tmp/previews guide-images

Needs google-chrome (or chromium); drives it over the DevTools protocol.
"""
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
import websockets

SRC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
# Regular users don't see the Admin link.
PREP = "document.querySelectorAll('.nav a[href=\"/admin\"]').forEach(a => a.remove());"
SHOTS = {
    # name: (page, extra js, js returning the bottom y of what to show)
    "board": ("board.html", "", "Math.max(...[...document.querySelectorAll('.server-card')].slice(0,6).map(e=>e.getBoundingClientRect().bottom))"),
    "book": ("book.html", "", "document.querySelector('.legend').getBoundingClientRect().bottom"),
    "me": ("me.html", "", "document.querySelector('.booking').getBoundingClientRect().bottom"),
    "end-confirm": ("me.html", "[...document.querySelectorAll('button')].find(b=>b.textContent.trim()=='End now').click();",
                    "Math.min(window.innerHeight, 560) - 24", 560),
    "calendar": ("calendar.html", "", "document.querySelector('.cal-legend').getBoundingClientRect().bottom"),
}
APP_JS = Path("app/web/static/app.js").read_text()

async def main():
    port = 9333
    prof = tempfile.mkdtemp()
    chrome = subprocess.Popen([shutil.which("google-chrome") or shutil.which("chromium"), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                               f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
                               "--window-size=1280,1400", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page"); break
            except Exception:
                time.sleep(0.2)
        async with websockets.connect(ws_url, max_size=None) as ws:
            n = 0
            async def call(method, **params):
                nonlocal n; n += 1; mid = n
                await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        return msg.get("result", {})
            async def ev(expr):
                r = await call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
                return r.get("result", {}).get("value")
            await call("Emulation.setDeviceMetricsOverride", width=1280, height=1400, deviceScaleFactor=1, mobile=False)
            for name, (page, extra, bottom, *vh) in SHOTS.items():
                await call("Emulation.setDeviceMetricsOverride", width=1280, height=vh[0] if vh else 1400,
                           deviceScaleFactor=1, mobile=False)
                html = (SRC / page).read_text().replace('<script src="/static/app.js" defer></script>', f"<script>{APP_JS}</script>")
                tmp = SRC / f"_shot_{name}.html"; tmp.write_text(html)
                await call("Page.navigate", url=f"file://{tmp}")
                await asyncio.sleep(0.8)
                await ev(PREP + extra)
                await asyncio.sleep(0.3)
                h = int(await ev(bottom)) + (24 if vh else 10)
                r = await call("Page.captureScreenshot", format="png",
                               clip={"x": 0, "y": 0, "width": 1280, "height": h, "scale": 1})
                (OUT / f"{name}.png").write_bytes(base64.b64decode(r["data"]))
                print(name, h)
    finally:
        chrome.terminate()
asyncio.run(main())
