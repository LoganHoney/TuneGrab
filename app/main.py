"""TuneGrab web API."""
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import categories, discover, library, lyrics, navidrome, scheduler, settings, store
from . import downloader as dl

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

@asynccontextmanager
async def lifespan(_app):
    dl.start_worker()
    scheduler.start()
    yield


app = FastAPI(title="TuneGrab", lifespan=lifespan)
STATIC = os.path.join(os.path.dirname(__file__), "static")


class AlbumReq(BaseModel):
    browseId: str
    videoIds: Optional[List[str]] = None  # download only these tracks


class SongReq(BaseModel):
    videoId: str
    title: Optional[str] = None


class UrlReq(BaseModel):
    urls: List[str]


class CookiesReq(BaseModel):
    text: str


class PathReq(BaseModel):
    path: str


class DeleteReq(BaseModel):
    paths: List[str]
    confirm: List[str]  # must repeat the paths, so a stray request can't delete anything


class ReplaceReq(BaseModel):
    path: str
    source: str  # YouTube video ID or URL


class LyricsReq(BaseModel):
    paths: Optional[List[str]] = None  # None = every song without lyrics
    force: bool = False


class ShiftReq(BaseModel):
    path: str
    seconds: float  # positive = lines show later



def _song(path):
    try:
        full = library.resolve(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not os.path.exists(full):
        raise HTTPException(404, "Song not found")
    return full


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


# the logo, home-screen icons (iOS only uses a PNG apple-touch-icon) and the web app manifest
ICONS = {"logo.svg": "image/svg+xml", "apple-touch-icon.png": "image/png", "icon-192.png": "image/png",
         "icon-512.png": "image/png", "manifest.webmanifest": "application/manifest+json"}


@app.get("/{name}", include_in_schema=False)
def icon(name: str):
    if name not in ICONS:
        raise HTTPException(404, "Not found")
    return FileResponse(os.path.join(STATIC, name), media_type=ICONS[name], headers={"Cache-Control": "max-age=86400"})


@app.get("/api/health")
def health():
    return {"ok": True, "format": settings.get("audio_format"), "music_dir": dl.MUSIC_DIR,
            "cookies": os.path.exists(dl.COOKIES), "pot_provider": bool(dl.POT_PROVIDER_URL)}


@app.get("/api/search")
def search(q: str, kind: str = "albums"):
    if not q.strip():
        return []
    try:
        return dl.search(q, kind)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"YouTube Music search failed: {e}")


@app.get("/api/album/{browse_id}")
def album(browse_id: str):
    try:
        return dl.album_details(browse_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not load album: {e}")


@app.post("/api/download/album")
def download_album(req: AlbumReq):
    return dl.enqueue_album(req.browseId, req.videoIds)


@app.post("/api/download/song")
def download_song(req: SongReq):
    return dl.enqueue_song(req.videoId, req.title)


@app.post("/api/download/urls")
def download_urls(req: UrlReq):
    if not any(u.strip() for u in req.urls):
        raise HTTPException(400, "No URLs given")
    return dl.enqueue_urls(req.urls)


@app.get("/api/jobs")
def jobs():
    return dl.list_jobs()


@app.delete("/api/jobs")
def clear_jobs():
    dl.clear_finished()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str):
    job = dl.retry_job(job_id)
    if not job:
        raise HTTPException(400, "Nothing to retry")
    return job


@app.get("/api/cookies")
def cookies_status():
    return dl.cookies_status()


@app.post("/api/cookies")
def cookies_upload(req: CookiesReq):
    try:
        dl.save_cookies(req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dl.cookies_status()


@app.delete("/api/cookies")
def cookies_delete():
    dl.delete_cookies()
    return dl.cookies_status()


# ---------------------------------------------------------------- library
@app.get("/api/library")
def library_list():
    tracks = library.list_tracks()
    added = {r["path"]: r["added"] for r in store.q("SELECT path, added FROM sources")}
    cats = categories.by_path()
    for t in tracks:
        t["backup"] = bool(library.backup_of(t["path"]))
        t["added"] = added.get(t["path"]) or t["mtime"]  # when TuneGrab downloaded it, else the file's date
        t["category"] = cats.get(t["path"])
    return tracks


# ---------------------------------------------------------------- categories (categories.py)
@app.get("/api/categories")
def categories_get():
    return categories.summary()


@app.post("/api/categories/song")
def categories_song(body: dict):
    try:
        categories.set_song(body.get("path"), body.get("category"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/categories/artist")
def categories_artist(body: dict):
    try:
        n = categories.set_artist(str(body.get("artist") or ""), body.get("category") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"songs": n}


@app.post("/api/categories/resort")
def categories_resort():
    if not categories.resort():
        raise HTTPException(409, "Already sorting, try again when it's done")
    return {"ok": True}


# ---------------------------------------------------------------- settings page
@app.get("/api/settings")
def settings_get():
    return {"values": settings.current(), "fields": settings.FIELDS}


@app.post("/api/settings")
def settings_set(changes: dict):
    try:
        return {"values": settings.update(changes), "fields": settings.FIELDS}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/library/cover")
def library_cover(path: str):
    data, mime = library.cover_bytes(_song(path))
    if not data:
        raise HTTPException(404, "No cover")
    return Response(data, media_type=mime, headers={"Cache-Control": "max-age=3600"})


@app.get("/api/library/lyrics")
def library_lyrics(path: str):
    return {"text": lyrics.read(_song(path))}


@app.post("/api/library/delete")
def library_delete(req: DeleteReq):
    if not req.paths:
        raise HTTPException(400, "No songs picked")
    if req.confirm != req.paths:
        raise HTTPException(400, "Confirmation didn't match")
    for p in req.paths:  # check them all before deleting any
        _song(p)
    for p in req.paths:
        vid = library.delete(p)
        if vid:
            dl.remove_archive(vid)
        navidrome.drop_from_cleanup(p)
    navidrome.scan()
    return {"ok": True, "deleted": len(req.paths)}


@app.post("/api/library/replace")
def library_replace(req: ReplaceReq):
    _song(req.path)
    if not req.source.strip():
        raise HTTPException(400, "Pick a song or paste a URL")
    return dl.enqueue_replace(req.path, req.source)


@app.post("/api/library/undo-replace")
def library_undo_replace(req: PathReq):
    _song(req.path)
    try:
        library.undo_replace(req.path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    navidrome.scan()
    return {"ok": True}


@app.post("/api/library/offload")
def library_offload(req: PathReq):
    _song(req.path)
    try:
        library.offload(req.path)
    except (RuntimeError, FileExistsError) as e:
        raise HTTPException(400, str(e))
    navidrome.drop_from_cleanup(req.path)
    navidrome.scan()
    return {"ok": True}


@app.get("/api/offloaded")
def offloaded():
    return {"available": library.offload_available(), "dir": library.OFFLOAD_DIR,
            "items": library.list_offloaded()}


@app.post("/api/offloaded/restore")
def offloaded_restore(req: PathReq):
    try:
        library.restore(req.path)
    except (ValueError, FileNotFoundError, FileExistsError) as e:
        raise HTTPException(400, str(e))
    navidrome.scan()
    return {"ok": True}


@app.post("/api/library/lyrics")
def library_fetch_lyrics(req: LyricsReq):
    for p in req.paths or []:
        _song(p)
    return dl.enqueue_lyrics(req.paths, req.force)


_shift_scan = None


@app.post("/api/library/lyrics/shift")
def library_shift_lyrics(req: ShiftReq):
    global _shift_scan
    if not 0 < abs(req.seconds) <= 30:
        raise HTTPException(400, "Shift by up to 30 seconds at a time")
    try:
        text = lyrics.shift(_song(req.path), req.seconds)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # one rescan once the nudging stops, not one per click
    if _shift_scan:
        _shift_scan.cancel()
    _shift_scan = threading.Timer(5, navidrome.scan)
    _shift_scan.start()
    return {"text": text}



# ---------------------------------------------------------------- playlists
@app.get("/api/playlists")
def playlists():
    return {
        "navidrome": navidrome.status(),
        "scheduler": scheduler.status(),
        "playlists": navidrome.playlists_status(),
    }



@app.get("/api/cleanup")
def cleanup():
    return {"report": navidrome.cleanup_status(), "scheduler": scheduler.status(),
            "offload_available": library.offload_available()}


@app.post("/api/cleanup/keep")
def cleanup_keep(req: PathReq):
    store.run("INSERT OR REPLACE INTO cleanup_keep(path, at) VALUES(?, ?)", req.path, time.time())
    navidrome.drop_from_cleanup(req.path)
    return {"ok": True}


@app.post("/api/playlists/rebuild")
def playlists_rebuild():
    if not navidrome.enabled():
        raise HTTPException(400, "Navidrome isn't configured")
    if not scheduler.start_task("playlists", scheduler.playlists):
        raise HTTPException(409, "Busy with another task, try again in a minute")
    return {"ok": True}


# ---------------------------------------------------------------- suggestions
@app.get("/api/suggestions")
def suggestions():
    return {"items": discover.list_suggestions(), "auto_add": settings.get("auto_add"),
            "status": scheduler.status()}


@app.post("/api/suggestions/refresh")
def suggestions_refresh():
    if not navidrome.enabled():
        raise HTTPException(400, "Navidrome isn't configured")
    if not scheduler.start_task("suggestions", scheduler.suggestions):
        raise HTTPException(409, "Busy with another task, try again in a minute")
    return {"ok": True}


@app.post("/api/suggestions/{video_id}/add")
def suggestion_add(video_id: str):
    try:
        return discover.accept(video_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/suggestions/{video_id}/dismiss")
def suggestion_dismiss(video_id: str):
    discover.dismiss(video_id)
    return {"ok": True}
