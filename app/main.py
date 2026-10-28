"""Stremio Remote: search Cinemeta from a phone and open the title on a Fire TV via ADB."""

import asyncio
import os
import re
import time
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
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")
CINEMETA = "https://v3-cinemeta.strem.io/catalog/{type}/top/search={q}.json"
CINEMETA_META = "https://v3-cinemeta.strem.io/meta/{type}/{id}.json"
OMDB = "https://www.omdbapi.com/"
RATINGS_TTL = 24 * 3600
ADB_TIMEOUT = 10
STATIC = Path(__file__).parent / "static"
IMDB_ID = re.compile(r"^tt\d+$")

app = FastAPI(title="Stremio Remote")
adb_lock = asyncio.Lock()
ratings_cache: dict[str, tuple[float, dict]] = {}
ratings_limit = asyncio.Semaphore(8)


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


async def fetch_ratings(client: httpx.AsyncClient, type_: str, imdb_id: str) -> dict:
    """IMDb score from Cinemeta, Rotten Tomatoes from OMDb when a key is set."""
    cached = ratings_cache.get(imdb_id)
    if cached and time.monotonic() - cached[0] < RATINGS_TTL:
        return cached[1]
    result = {"imdb": None, "rt": None}
    async with ratings_limit:
        try:
            r = await client.get(CINEMETA_META.format(type=type_, id=imdb_id))
            if r.status_code == 200:
                result["imdb"] = (r.json().get("meta") or {}).get("imdbRating") or None
        except httpx.HTTPError:
            pass
        if OMDB_API_KEY:
            try:
                r = await client.get(OMDB, params={"i": imdb_id, "apikey": OMDB_API_KEY})
                data = r.json() if r.status_code == 200 else {}
                if data.get("Response") == "True":
                    for rating in data.get("Ratings", []):
                        if rating.get("Source") == "Rotten Tomatoes":
                            result["rt"] = rating.get("Value")
                    if not result["imdb"] and data.get("imdbRating") not in (None, "N/A"):
                        result["imdb"] = data["imdbRating"]
            except (httpx.HTTPError, ValueError):
                pass
    ratings_cache[imdb_id] = (time.monotonic(), result)
    return result


@app.get("/api/ratings")
async def ratings(items: str):
    """items: comma-separated type:id pairs, e.g. series:tt0903747,movie:tt1160419"""
    pairs = []
    for item in items.split(",")[:30]:
        type_, _, imdb_id = item.partition(":")
        if type_ in ("series", "movie") and IMDB_ID.match(imdb_id):
            pairs.append((type_, imdb_id))
    async with httpx.AsyncClient(timeout=8) as client:
        results = await asyncio.gather(*(fetch_ratings(client, t, i) for t, i in pairs))
    return {imdb_id: r for (_, imdb_id), r in zip(pairs, results)}


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
