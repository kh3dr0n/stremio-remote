"""Stremio Remote: search Cinemeta from a phone and open the title on a Fire TV via ADB."""

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

FIRETV_HOST = os.environ.get("FIRETV_HOST", "")
if not FIRETV_HOST:
    raise SystemExit("FIRETV_HOST is not set (example: FIRETV_HOST=192.168.1.50:5555)")
if ":" not in FIRETV_HOST:
    FIRETV_HOST += ":5555"
CINEMETA = "https://v3-cinemeta.strem.io/catalog/{type}/top/search={q}.json"
ADB_TIMEOUT = 10
STATIC = Path(__file__).parent / "static"
IMDB_ID = re.compile(r"^tt\d+$")

app = FastAPI(title="Stremio Remote")
adb_lock = asyncio.Lock()


async def adb(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), ADB_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "adb timed out"
    return proc.returncode, out.decode(errors="replace").strip()


async def ensure_connected() -> tuple[bool, str]:
    """Connect if needed. Returns (ok, state) where state is device/unauthorized/offline."""
    _, state = await adb("-s", FIRETV_HOST, "get-state")
    if state == "device":
        return True, state
    await adb("disconnect", FIRETV_HOST)
    _, out = await adb("connect", FIRETV_HOST)
    _, state = await adb("-s", FIRETV_HOST, "get-state")
    if state == "device":
        return True, state
    if "unauthorized" in out or "authenticate" in out or "unauthorized" in state:
        return False, "unauthorized"
    return False, "offline"


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/search")
async def search(q: str):
    q = q.strip()
    if len(q) < 2:
        return []
    async with httpx.AsyncClient(timeout=8) as client:
        responses = await asyncio.gather(
            *(client.get(CINEMETA.format(type=t, q=quote(q))) for t in ("series", "movie")),
            return_exceptions=True,
        )
    lists = []
    for r in responses:
        if isinstance(r, Exception) or r.status_code != 200:
            lists.append([])
            continue
        lists.append(r.json().get("metas", []))
    # Interleave series and movies so the best match of each type is near the top
    merged = []
    for i in range(max(map(len, lists), default=0)):
        for metas in lists:
            if i < len(metas):
                merged.append(metas[i])
    return [
        {
            "id": m["id"],
            "type": m["type"],
            "name": m.get("name", ""),
            "year": m.get("releaseInfo") or m.get("year") or "",
            "poster": m.get("poster", ""),
        }
        for m in merged
        if IMDB_ID.match(m.get("id", ""))
    ][:30]


class OpenRequest(BaseModel):
    id: str
    type: str


@app.post("/api/open")
async def open_on_tv(req: OpenRequest):
    if not IMDB_ID.match(req.id) or req.type not in ("series", "movie"):
        raise HTTPException(400, "Invalid title")
    async with adb_lock:
        ok, state = await ensure_connected()
        if not ok:
            raise HTTPException(503, f"Fire TV {state}")
        await adb("-s", FIRETV_HOST, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
        code, out = await adb(
            "-s", FIRETV_HOST, "shell", "am", "start",
            "-a", "android.intent.action.VIEW",
            "-d", f"stremio:///detail/{req.type}/{req.id}",
        )
    if code != 0 or "Error" in out:
        raise HTTPException(502, out or "adb failed")
    return {"ok": True}


@app.get("/api/status")
async def status():
    async with adb_lock:
        ok, state = await ensure_connected()
    return {"connected": ok, "state": state, "host": FIRETV_HOST}
