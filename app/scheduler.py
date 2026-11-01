"""Background loop: playlist rebuilds, suggestions, housekeeping."""
import logging
import os
import threading
import time

from . import categories, discover, library, navidrome, settings, store
from . import downloader as dl

log = logging.getLogger("tunegrab.scheduler")

POLL_SECONDS = float(os.environ.get("NOWPLAYING_POLL_SECONDS", "10"))
PLAYLIST_HOURS = float(os.environ.get("PLAYLIST_REFRESH_HOURS", "6"))
SUGGEST_HOURS = float(os.environ.get("SUGGEST_REFRESH_HOURS", "24"))

_task_lock = threading.Lock()
state = {"task": None, "detail": "", "errors": {}}


def run_task(name, fn):
    """Run one task at a time; returns False if something else is already running."""
    if not _task_lock.acquire(blocking=False):
        return False
    state.update(task=name, detail="")
    try:
        fn()
        state["errors"].pop(name, None)
    except Exception as e:  # noqa: BLE001
        log.exception("%s failed", name)
        state["errors"][name] = str(e)[:300]
    finally:
        # failures wait for the next window too (or a manual "Run now"), rather than retrying every loop
        store.put(f"last_{name}", time.time())
        state.update(task=None, detail="")
        _task_lock.release()
    return True


def start_task(name, fn):
    if _task_lock.locked():
        return False
    threading.Thread(target=run_task, args=(name, fn), daemon=True).start()
    return True


def playlists():
    navidrome.build_playlists()


def suggestions():
    discover.refresh(progress=lambda d: state.update(detail=d))


def backfill_sources():
    """Older downloads don't record which YouTube video they came from. Match them up by title so
    deleting one lets it be downloaded again, and suggestions can use them as seeds."""
    known = {r["video_id"] for r in store.q("SELECT video_id FROM sources")}
    todo = [v for v in sorted(dl._archive) if v not in known]
    if not todo:
        return
    tracks = [t for t in library.list_tracks() if not t["video_id"]]
    for vid in todo:
        try:
            with dl._ytm_lock:
                vd = dl.ytm().get_song(vid).get("videoDetails") or {}
        except Exception:  # noqa: BLE001
            continue
        title, author = library.norm(vd.get("title")), library.norm(dl.strip_topic(vd.get("author")))
        hits = [t for t in tracks if library.norm(t["title"]) == title
                and (author in library.norm(t["artist"]) or author in library.norm(t["album_artist"]))]
        if len(hits) == 1:
            store.run("INSERT OR IGNORE INTO sources(path, video_id, added) VALUES(?,?,?)",
                      hits[0]["path"], vid, time.time())
            tracks.remove(hits[0])
        time.sleep(0.5)
    log.info("matched %d older downloads to their YouTube videos", len(todo))


def _poll_loop():
    backoff = POLL_SECONDS
    while True:
        try:
            navidrome.skip_log.poll()
            backoff = POLL_SECONDS
        except Exception as e:  # noqa: BLE001
            log.debug("now-playing poll failed: %s", e)
            backoff = min(backoff * 2, 300)  # Navidrome down: back off instead of spamming it
        time.sleep(backoff)


def _due(name, hours):
    last = float(store.get(f"last_{name}", 0) or 0)
    return time.time() - last >= hours * 3600


def _maintenance_loop():
    time.sleep(30)
    if not store.get("last_backfill"):
        run_task("backfill", backfill_sources)
    while True:
        categories.start()   # sorts new songs into categories (does nothing when all are sorted)
        if navidrome.enabled():
            if _due("playlists", PLAYLIST_HOURS):
                run_task("playlists", playlists)
            if SUGGEST_HOURS > 0 and _due("suggestions", SUGGEST_HOURS):
                run_task("suggestions", suggestions)
        if settings.get("lyrics_auto") and _due("lyrics_retry", 24):
            # picks up songs whose lyrics lookup was put off while LRCLIB was down
            store.put("last_lyrics_retry", time.time())
            if dl.missing_lyrics():
                dl.enqueue_lyrics()
        if _due("purge", 24):
            run_task("purge", library.purge_backups)
        time.sleep(300)


def start():
    if navidrome.enabled() and navidrome.SKIP_LOG:
        threading.Thread(target=_poll_loop, daemon=True, name="skiplog").start()
    threading.Thread(target=_maintenance_loop, daemon=True, name="maintenance").start()


def status():
    return {"task": state["task"], "detail": state["detail"], "errors": state["errors"],
            "last_playlists": float(store.get("last_playlists", 0) or 0) or None,
            "last_suggestions": float(store.get("last_suggestions", 0) or 0) or None}
