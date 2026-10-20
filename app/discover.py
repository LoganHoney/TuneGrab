"""Song suggestions from listening history, via YouTube Music radio. Explicit songs never get through."""
import logging
import re
import time

from . import downloader as dl
from . import library, navidrome, settings, store

log = logging.getLogger("tunegrab.discover")

_EXPLICIT_WORDS = re.compile(r"\bexplicit\b|\(dirty\)|\buncensored\b", re.I)


def is_clean(video_id, artist, title):
    """True only when YouTube Music says this exact track isn't explicit. Unknown counts as explicit."""
    if _EXPLICIT_WORDS.search(title or ""):
        return False
    with dl._ytm_lock:
        hits = dl.ytm().search(f"{artist} {title}", filter="songs", limit=10)
    for h in hits:
        if h.get("videoId") == video_id:
            return h.get("isExplicit") is False
    return False


def _library_keys():
    """Songs already owned, from the files on disk and from Navidrome's own view of the library."""
    keys = set()
    for t in library.list_tracks():
        keys.add(library.norm(t["title"]) + "|" + library.norm(t["artist"].split(",")[0]))
    for s in navidrome.all_songs():
        keys.add(library.norm(s.get("title")) + "|" + library.norm((s.get("artist") or "").split(",")[0]))
    return keys


def _seed_video_id(song):
    vid = library.video_id_of(song.get("path") or "")
    if vid:
        return vid
    with dl._ytm_lock:
        hits = dl.ytm().search(f"{song.get('artist')} {song.get('title')}", filter="songs", limit=5)
    want = library.norm(song.get("title"))
    for h in hits:
        if library.norm(h.get("title")) == want:
            return h["videoId"]
    return None


def refresh(progress=None):
    seeds = navidrome.top_seeds()
    have = _library_keys()
    known = {r["video_id"]: r["status"] for r in store.q("SELECT video_id, status FROM suggestions")}
    pool = {}
    for seed in seeds:
        if progress:
            progress(f"Listening to radio for {seed.get('title')}")
        vid = _seed_video_id(seed)
        if not vid:
            continue
        with dl._ytm_lock:
            radio = dl.ytm().get_watch_playlist(videoId=vid, radio=True, limit=30).get("tracks") or []
        for t in radio:
            v = t.get("videoId")
            artist = dl.join_artists(t.get("artists"))
            key = library.norm(t.get("title")) + "|" + library.norm(dl.first_artist(t.get("artists")))
            # official audio only; skip anything already owned, offered before, or dismissed
            if (not v or v == vid or t.get("videoType") != "MUSIC_VIDEO_TYPE_ATV" or key in have
                    or dl.in_archive(v) or v in known):
                continue
            c = pool.setdefault(v, {"video_id": v, "title": t.get("title"), "artist": artist,
                                    "album": (t.get("album") or {}).get("name") or "",
                                    "thumb": dl.best_thumb(t.get("thumbnail"), 226),
                                    "duration": t.get("length") or "", "score": 0, "because": []})
            c["score"] += 1
            if seed.get("title") not in c["because"]:
                c["because"].append(seed.get("title"))
        time.sleep(1)

    ranked = sorted(pool.values(), key=lambda c: -c["score"])
    added = 0
    for c in ranked:
        if added >= settings.get("max_suggestions"):
            break
        if progress:
            progress(f"Checking {c['artist']} - {c['title']} for explicit content")
        try:
            clean = is_clean(c["video_id"], c["artist"].split(",")[0], c["title"])
        except Exception as e:  # noqa: BLE001
            log.warning("explicit check failed for %s: %s", c["title"], e)
            clean = False
        time.sleep(0.5)
        if not clean:
            store.run("INSERT OR IGNORE INTO suggestions(video_id, title, artist, status, at) VALUES(?,?,?,?,?)",
                      c["video_id"], c["title"], c["artist"], "explicit", time.time())
            continue
        reason = "Because you play " + ", ".join(c["because"][:2])
        store.run("INSERT OR IGNORE INTO suggestions(video_id, title, artist, album, thumb, duration, reason, "
                  "score, status, at) VALUES(?,?,?,?,?,?,?,?,'new',?)", c["video_id"], c["title"], c["artist"],
                  c["album"], c["thumb"], c["duration"], reason, c["score"], time.time())
        added += 1
    # old suggestions you never acted on make room for new ones
    store.run("DELETE FROM suggestions WHERE status='new' AND at < ?", time.time() - 21 * 86400)
    store.put("suggestions_at", time.time())

    auto = 0
    auto_add = settings.get("auto_add")  # songs added on their own per refresh; 0 = off
    if auto_add > 0:
        for s in store.q("SELECT * FROM suggestions WHERE status='new' ORDER BY score DESC, at DESC LIMIT ?", auto_add):
            accept(s["video_id"], auto=True)
            auto += 1
    return {"new": added, "auto_added": auto}


def list_suggestions():
    return store.q("SELECT * FROM suggestions WHERE status IN ('new', 'added') ORDER BY status='added', score DESC, at DESC LIMIT 60")


def accept(video_id, auto=False):
    s = store.one("SELECT * FROM suggestions WHERE video_id=?", video_id)
    if not s or s["status"] == "explicit":
        raise ValueError("Unknown or explicit suggestion")
    store.run("UPDATE suggestions SET status='added', reason=? WHERE video_id=?",
              ("Added automatically. " if auto else "") + (s["reason"] or ""), video_id)
    return dl.enqueue_song(video_id, f"{s['artist']} - {s['title']}", clean_only=True, discovery=True)


def dismiss(video_id):
    store.run("UPDATE suggestions SET status='dismissed' WHERE video_id=?", video_id)
