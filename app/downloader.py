"""TuneGrab core: YouTube Music search, job queue, download + tagging."""
import logging
import os
import queue
import random
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

import mutagen
import yt_dlp
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError
from ytmusicapi import YTMusic

from . import library, lyrics, navidrome, settings, store

log = logging.getLogger("tunegrab")

# ---------------------------------------------------------------- settings
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
AUDIO_QUALITY = os.environ.get("AUDIO_QUALITY", "0")  # mp3: 0 = best VBR (V0), or e.g. 320
# Random pause between tracks so downloads don't look like a script hammering YouTube
SLEEP_MIN = float(os.environ.get("SLEEP_MIN", "5"))
SLEEP_MAX = max(SLEEP_MIN, float(os.environ.get("SLEEP_MAX", "15")))
# Small pause between the individual requests yt-dlp makes for each track
SLEEP_REQUESTS = float(os.environ.get("SLEEP_REQUESTS", "1"))
SINGLES_ALBUM = os.environ.get("SINGLES_ALBUM_NAME", "Singles")

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "").rstrip("/")

COOKIES = os.path.join(DATA_DIR, "cookies.txt")
ARCHIVE = os.path.join(DATA_DIR, "archive.txt")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
for d in (DATA_DIR, TMP_DIR, CACHE_DIR):
    os.makedirs(d, exist_ok=True)

# ---------------------------------------------------------------- helpers
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_VID = re.compile(r"(?:v=|youtu\.be/|shorts/|/embed/)([\w-]{11})")


def clean(s, fallback="Unknown"):
    s = _BAD.sub("_", (s or "").strip()).strip(" .")
    return s[:150] or fallback


def join_artists(artists):
    if not artists:
        return ""
    return ", ".join(a.get("name", "") for a in artists if a.get("name"))


def first_artist(artists):
    for a in artists or []:
        if a.get("name"):
            return a["name"]
    return ""


def best_thumb(thumbs, size=544):
    if not thumbs:
        return ""
    url = thumbs[-1].get("url", "")
    # googleusercontent thumbnails can be resized with =wN-hN
    return re.sub(r"=w\d+-h\d+.*$", f"=w{size}-h{size}-l90-rj", url)


def fetch_bytes(url, timeout=20):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        log.warning("cover fetch failed: %s", e)
        return None


def image_mime(data):
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


class BotCheck(Exception):
    """YouTube asked us to prove we're not a bot. Continuing just digs the hole deeper."""


BOT_MSG = ("YouTube bot check. Upload a cookies.txt on the Settings tab, "
           "then press Retry failed.")


def is_bot_error(e):
    t = str(e).lower()
    return "not a bot" in t or "sign in to confirm" in t


def cookies_status():
    if not os.path.exists(COOKIES):
        return {"present": False}
    return {"present": True, "updated": os.path.getmtime(COOKIES)}


def save_cookies(text):
    text = (text or "").replace("\r\n", "\n").strip()
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines or not all(len(ln.split("\t")) >= 7 for ln in lines[:20]):
        raise ValueError("That doesn't look like a Netscape-format cookies.txt file.")
    if "youtube.com" not in text:
        raise ValueError("No youtube.com cookies found in that file.")
    if not text.startswith("# Netscape HTTP Cookie File") and not text.startswith("# HTTP Cookie File"):
        text = "# Netscape HTTP Cookie File\n" + text
    with open(COOKIES, "w") as fh:
        fh.write(text + "\n")
    os.chmod(COOKIES, 0o600)


def delete_cookies():
    if os.path.exists(COOKIES):
        os.remove(COOKIES)


def strip_topic(name):
    return re.sub(r"\s*-\s*Topic$", "", name or "")


# ---------------------------------------------------------------- YouTube Music
_ytm = None
_ytm_lock = threading.Lock()


def ytm():
    global _ytm
    if _ytm is None:
        _ytm = YTMusic()
    return _ytm


def owned(video_id, title, artist, keys):
    return in_archive(video_id) or (library.norm(title) + "|" + library.norm((artist or "").split(",")[0])) in keys


def search(query, kind="albums", limit=20):
    """Results the user can download, minus explicit ones (clean-only) and versions they chose to hide."""
    kind = "songs" if kind == "songs" else "albums"
    with _ytm_lock:
        results = ytm().search(query, filter=kind, limit=limit)
    clean, keys = settings.get("clean_only"), library.owned_keys() if kind == "songs" else set()
    out = []
    for r in results:
        if (clean and r.get("isExplicit")) or settings.unwanted_version(r.get("title"), join_artists(r.get("artists"))):
            continue
        if kind == "albums" and r.get("browseId"):
            out.append({
                "kind": "album",
                "browseId": r["browseId"],
                "title": r.get("title"),
                "artist": join_artists(r.get("artists")),
                "year": r.get("year"),
                "type": r.get("type") or "Album",
                "thumb": best_thumb(r.get("thumbnails"), 300),
            })
        elif kind == "songs" and r.get("videoId"):
            album = r.get("album") or {}
            out.append({
                "kind": "song",
                "videoId": r["videoId"],
                "title": r.get("title"),
                "artist": join_artists(r.get("artists")),
                "album": album.get("name"),
                "albumId": album.get("id"),
                "duration": r.get("duration"),
                "thumb": best_thumb(r.get("thumbnails"), 300),
                "owned": owned(r["videoId"], r.get("title"), join_artists(r.get("artists")), keys),
            })
    return out


def album_details(browse_id):
    with _ytm_lock:
        a = ytm().get_album(browse_id)
    album_artist = first_artist(a.get("artists")) or "Unknown Artist"
    keys = library.owned_keys()
    tracks = []
    for i, t in enumerate(a.get("tracks") or [], 1):
        artist = join_artists(t.get("artists")) or album_artist
        tracks.append({
            "videoId": t.get("videoId"),
            "title": t.get("title"),
            "artist": join_artists(t.get("artists")) or album_artist,
            "n": t.get("trackNumber") or i,
            "duration": t.get("duration"),
            "available": bool(t.get("videoId")) and t.get("isAvailable", True) is not False,
            "explicit": t.get("isExplicit"),
            "video_type": t.get("videoType"),
            # pre-ticked unless already owned, a version the user hides, or explicit in clean-only mode
            "owned": owned(t.get("videoId"), t.get("title"), artist, keys),
            "unwanted": settings.unwanted_version(t.get("title"), artist)
                        or bool(settings.get("clean_only") and t.get("isExplicit")),
        })
    return {
        "browseId": browse_id,
        "title": a.get("title"),
        "artist": album_artist,
        "year": a.get("year"),
        "type": a.get("type") or "Album",
        "thumb": best_thumb(a.get("thumbnails"), 1200),
        "tracks": tracks,
    }


def _secs(text):
    try:
        parts = [int(p) for p in (text or "").split(":")]
    except ValueError:
        return 0
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


_LYRIC_VIDEO = re.compile(r"\blyrics?\b", re.I)


def audio_version(video_id, artist, title, duration=""):
    """Some album tracks point at the music video, whose audio carries the video's intro and outro.
    Returns the official audio upload of the same song, else the artist's own lyric video, else video_id."""
    want, who, length = library.norm(title), library.norm(artist.split(",")[0]), _secs(duration)

    def by_artist(r):
        return any(library.norm(a.get("name")) == who for a in r.get("artists") or [])

    def closest(hits):
        hits = [h for h in hits if not length or abs(_secs(h.get("duration")) - length) <= 10]
        return min(hits, key=lambda h: abs(_secs(h.get("duration")) - length), default=None)

    with _ytm_lock:
        songs = ytm().search(f"{artist} {title}", filter="songs", limit=10)
    hit = closest([s for s in songs if s.get("videoType") == "MUSIC_VIDEO_TYPE_ATV"
                   and library.norm(s.get("title")) == want and by_artist(s)])
    if not hit:
        # OMV = uploaded by the artist's official channel, so fan-made lyric videos never qualify
        with _ytm_lock:
            videos = ytm().search(f"{artist} {title} lyric video", filter="videos", limit=10)
        hit = closest([v for v in videos if v.get("videoType") == "MUSIC_VIDEO_TYPE_OMV" and by_artist(v)
                       and want in library.norm(v.get("title")) and _LYRIC_VIDEO.search(v.get("title") or "")
                       and v.get("videoId") != video_id])
    if hit:
        log.info("%s - %s: using %s instead of music video %s", artist, title, hit["videoId"], video_id)
        return hit["videoId"]
    return video_id


# ---------------------------------------------------------------- archive
_archive_lock = threading.Lock()
_archive = set()
if os.path.exists(ARCHIVE):
    with open(ARCHIVE) as fh:
        _archive = {line.strip() for line in fh if line.strip()}


def in_archive(vid):
    with _archive_lock:
        return vid in _archive


def add_archive(vid):
    with _archive_lock:
        if vid and vid not in _archive:
            _archive.add(vid)
            with open(ARCHIVE, "a") as fh:
                fh.write(vid + "\n")


def remove_archive(vid):
    """Forget a download so the song can be fetched again after it was deleted."""
    with _archive_lock:
        if vid not in _archive:
            return
        _archive.discard(vid)
        with open(ARCHIVE, "w") as fh:
            fh.writelines(v + "\n" for v in _archive)


# ---------------------------------------------------------------- jobs
_jobs = {}
_jobs_lock = threading.Lock()
_q = queue.Queue()


def _new_job(kind, title, payload):
    job = {
        "id": uuid.uuid4().hex[:10],
        "kind": kind,
        "title": title,
        "status": "queued",
        "error": "",
        "items": [],
        "created": time.time(),
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
        # keep the last 100 jobs
        for old in sorted(_jobs.values(), key=lambda j: j["created"])[:-100]:
            _jobs.pop(old["id"], None)
    _q.put((job, payload))
    return job


def enqueue_album(browse_id, video_ids=None):
    return _new_job("album", f"Album {browse_id}", {"browseId": browse_id, "only": video_ids})


def enqueue_song(video_id, title=None, clean_only=False, discovery=False):
    return _new_job("song", title or video_id,
                    {"videoId": video_id, "clean_only": clean_only, "discovery": discovery})


def enqueue_replace(path, source):
    """source: a YouTube video ID or URL whose audio replaces the file at path (tags are kept)."""
    info = library.read_info(library.resolve(path))
    return _new_job("replace", f"Replace {info['artist']} - {info['title']}", {"path": path, "source": source})


def enqueue_lyrics(paths=None, force=False):
    """paths=None: every song that has no lyrics yet (and wasn't already searched without luck)."""
    title = "Lyrics for missing songs" if paths is None else f"Lyrics for {len(paths)} song(s)"
    return _new_job("lyrics", title, {"paths": paths, "force": force})


def enqueue_urls(urls):
    urls = [u.strip() for u in urls if u.strip()]
    return _new_job("urls", f"{len(urls)} URL(s)", {"urls": urls})


def list_jobs():
    with _jobs_lock:
        return sorted((dict(j, items=list(j["items"])) for j in _jobs.values()),
                      key=lambda j: j["created"], reverse=True)


def clear_finished():
    with _jobs_lock:
        for jid in [k for k, j in _jobs.items() if j["status"] in ("done", "error")]:
            _jobs.pop(jid)


# ---------------------------------------------------------------- download + tag
def _ydl_opts(tmp, download=True, fmt="mp3"):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "cachedir": CACHE_DIR,
        "sleep_interval_requests": SLEEP_REQUESTS,
    }
    if download:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt,
            "preferredquality": AUDIO_QUALITY,
        }]
    if os.path.exists(COOKIES):
        opts["cookiefile"] = COOKIES
    if POT_PROVIDER_URL:
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}}
    return opts


def tag_file(path, m, cover):
    total = m.get("total")
    trck = f"{m['n']}/{total}" if m.get("n") and total else (str(m["n"]) if m.get("n") else None)
    if path.endswith(".mp3"):
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(TIT2(encoding=3, text=m["title"]))
        tags.add(TPE1(encoding=3, text=m["artist"]))
        tags.add(TPE2(encoding=3, text=m["album_artist"]))
        tags.add(TALB(encoding=3, text=m["album"]))
        if trck:
            tags.add(TRCK(encoding=3, text=trck))
        if m.get("year"):
            tags.add(TDRC(encoding=3, text=str(m["year"])))
        mime = image_mime(cover)
        if mime:
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=3)
        return
    f = mutagen.File(path, easy=True)
    if f is None:
        return
    f["title"] = m["title"]
    f["artist"] = m["artist"]
    f["albumartist"] = m["album_artist"]
    f["album"] = m["album"]
    if trck:
        f["tracknumber"] = trck
    if m.get("year"):
        f["date"] = str(m["year"])
    f.save()


def download_track(spec, item):
    """spec keys (all optional except video_id/url): title, artist, album_artist,
    album, year, n, total, cover (bytes)"""
    url = spec.get("url") or f"https://music.youtube.com/watch?v={spec['video_id']}"
    vid = spec.get("video_id")
    if not vid:
        mm = _VID.search(url)
        vid = mm.group(1) if mm else None
    if vid and in_archive(vid):
        item.update(status="skipped", message="already downloaded")
        return False

    tmp = tempfile.mkdtemp(dir=TMP_DIR)
    try:
        item.update(status="downloading")
        src, info = _fetch_audio(url, tmp)
        vid = info.get("id") or vid

        info_artists = info.get("artists") or []
        artist = (spec.get("artist") or info.get("artist") or ", ".join(info_artists)
                  or strip_topic(info.get("uploader")) or "Unknown Artist")
        album_artist = (spec.get("album_artist") or (info_artists[0] if info_artists else None)
                        or artist.split(",")[0].strip())
        m = {
            "title": spec.get("title") or info.get("track") or info.get("title") or vid,
            "artist": artist,
            "album_artist": album_artist,
            "album": spec.get("album") or info.get("album") or SINGLES_ALBUM,
            "year": spec.get("year") or info.get("release_year") or (info.get("upload_date") or "")[:4] or None,
            "n": spec.get("n"),
            "total": spec.get("total"),
        }
        cover = spec.get("cover") or fetch_bytes(info.get("thumbnail"))
        item["title"] = f"{m['artist']} - {m['title']}"

        dest_dir = os.path.join(MUSIC_DIR, clean(m["album_artist"]), clean(m["album"]))
        os.makedirs(dest_dir, exist_ok=True)
        prefix = f"{int(m['n']):02d} - " if m.get("n") else ""
        dest = os.path.join(dest_dir, f"{prefix}{clean(m['title'])}{os.path.splitext(src)[1]}")

        tag_file(src, m, cover)
        shutil.move(src, dest)
        add_archive(vid)
        library.set_video_id(dest, vid)
        rel = os.path.relpath(dest, MUSIC_DIR)
        msg = rel
        if settings.get("lyrics_auto"):
            try:
                found = lyrics.fetch_for(dest, vid)
                msg += f" · lyrics from {found}" if found else " · no lyrics found"
                if not found:
                    store.run("INSERT OR REPLACE INTO lyrics_misses(path, at) VALUES(?,?)", rel, time.time())
            except lyrics.LrclibDown:
                msg += " · lyrics later (LRCLIB unavailable)"
            except Exception as e:  # noqa: BLE001
                log.warning("lyrics failed for %s: %s", rel, e)
        item.update(status="done", message=msg, path=rel)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch_audio(url, tmp):
    fmt = settings.get("audio_format")  # read per download, so a change on the Settings page applies right away
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(tmp, fmt=fmt)) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # noqa: BLE001
        if is_bot_error(e):
            raise BotCheck(BOT_MSG) from e
        raise
    files = [f for f in os.listdir(tmp) if f.endswith("." + fmt)]
    if not files:
        raise RuntimeError("no audio file produced")
    return os.path.join(tmp, files[0]), info


def _source_url(source):
    source = source.strip()
    if re.fullmatch(r"[\w-]{11}", source):
        return f"https://music.youtube.com/watch?v={source}"
    if not source.startswith(("http://", "https://")):
        raise ValueError("Give a YouTube video ID or URL")
    return source


def _run_replace(job, path, source):
    full = library.resolve(path)
    item = {"title": job["title"][len("Replace "):], "status": "downloading", "message": "", "src": source}
    job["items"] = [item]
    job["retry"] = {"kind": "replace", "path": path}
    tmp = tempfile.mkdtemp(dir=TMP_DIR)
    try:
        try:
            src, info = _fetch_audio(_source_url(source), tmp)
        except Exception as e:  # noqa: BLE001
            item.update(status="error", message=str(e)[:300])
            return
        old_vid = library.video_id_of(path)
        library.backup_for_replace(full)
        library.copy_tags(full, src)  # title, artist, album, cover, lyrics... all carried over
        dest = os.path.splitext(full)[0] + os.path.splitext(src)[1]
        shutil.move(src, dest)
        if dest != full:
            os.remove(full)
        library.forget(full)
        library.touch_dir(dest)
        vid = info.get("id")
        if old_vid and old_vid != vid:
            remove_archive(old_vid)
        if vid:
            add_archive(vid)
            library.set_video_id(dest, vid)
        item.update(status="done", path=library.relpath(dest),
                    message=f"replaced with {info.get('title') or vid}; old version kept for "
                            f"{int(library.BACKUP_DAYS)} days")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def missing_lyrics(force=False):
    """Songs with no lyrics yet; without force, leaves out the ones already searched without luck."""
    misses = {r["path"] for r in store.q("SELECT path FROM lyrics_misses")}
    return [t["path"] for t in library.list_tracks()
            if not t["lyrics"] and (force or t["path"] not in misses)]


def _run_lyrics(job, paths, force):
    if paths is None:
        paths = missing_lyrics(force)
    items = [{"title": p, "status": "queued", "message": "", "src": p} for p in paths]
    job["items"] = items
    job["retry"] = {"kind": "lyrics"}
    for item in items:
        try:
            full = library.resolve(item["src"])
            if not force and library.has_lyrics(full):
                item.update(status="skipped", message="already has lyrics")
                continue
            info = library.read_info(full)
            item.update(status="downloading", title=f"{info['artist']} - {info['title']}")
            found = lyrics.fetch_for(full)
            if found:
                store.run("DELETE FROM lyrics_misses WHERE path=?", item["src"])
                item.update(status="done", message=f"from {found}")
            else:
                store.run("INSERT OR REPLACE INTO lyrics_misses(path, at) VALUES(?,?)", item["src"], time.time())
                item.update(status="skipped", message="no lyrics found")
        except lyrics.LrclibDown as e:
            item.update(status="skipped", message=str(e))
        except Exception as e:  # noqa: BLE001
            item.update(status="error", message=str(e)[:300])
        time.sleep(1)  # be polite to LRCLIB


def _pause():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def _run_album(job, browse_id, only=None):
    alb = album_details(browse_id)
    job["title"] = f"{alb['artist']} - {alb['title']} ({alb.get('year') or '?'})"
    cover = fetch_bytes(alb["thumb"])
    dest_dir = os.path.join(MUSIC_DIR, clean(alb["artist"]), clean(alb["title"]))
    os.makedirs(dest_dir, exist_ok=True)
    cover_path = os.path.join(dest_dir, "cover.jpg")
    if image_mime(cover) == "image/jpeg" and not os.path.exists(cover_path):
        with open(cover_path, "wb") as fh:
            fh.write(cover)

    tracks = [t for t in alb["tracks"] if not only or t["videoId"] in only]
    items = [{"title": f"{int(t['n']):02d}. {t['title']}", "status": "queued", "message": "",
              "src": t["videoId"]} for t in tracks]
    job["items"] = items
    job["retry"] = {"kind": "album", "browseId": browse_id}
    for idx, (t, item) in enumerate(zip(tracks, items)):
        if not t["available"]:
            item.update(status="skipped", message="not available on YouTube Music")
            continue
        if t.get("explicit") and settings.get("clean_only"):
            item.update(status="skipped", message="explicit (clean-only is on in Settings)")
            continue
        try:
            vid = t["videoId"]
            if t.get("video_type") not in (None, "MUSIC_VIDEO_TYPE_ATV") and not in_archive(vid):
                try:
                    vid = audio_version(vid, t["artist"], t["title"], t.get("duration"))
                except Exception as e:  # noqa: BLE001
                    log.warning("audio lookup failed for %s: %s", t["title"], e)
            did = download_track({
                "video_id": vid, "title": t["title"], "artist": t["artist"],
                "album_artist": alb["artist"], "album": alb["title"], "year": alb.get("year"),
                "n": t["n"], "total": len(alb["tracks"]), "cover": cover,
            }, item)
            if did:
                _pause()
        except BotCheck as e:
            _stop_after_bot(items[idx:], str(e))
            return
        except Exception as e:  # noqa: BLE001
            log.exception("track failed")
            item.update(status="error", message=str(e)[:300])


def _run_song(job, video_id, clean_only=False, discovery=False):
    clean_only = clean_only or settings.get("clean_only")
    with _ytm_lock:
        song = ytm().get_song(video_id)
    vd = song.get("videoDetails", {})
    item = {"title": vd.get("title") or video_id, "status": "queued", "message": "", "src": video_id}
    job["retry"] = {"kind": "song"}
    job["items"] = [item]
    spec = {"video_id": video_id}
    # Look up the album so the song lands in the right folder with a track number
    try:
        with _ytm_lock:
            wp = ytm().get_watch_playlist(videoId=video_id, limit=1)
        tr = (wp.get("tracks") or [{}])[0]
        album_id = (tr.get("album") or {}).get("id")
        if album_id:
            alb = album_details(album_id)
            for t in alb["tracks"]:
                if t["videoId"] == video_id or (t["title"] or "").lower() == (tr.get("title") or "").lower():
                    if clean_only and t.get("explicit"):
                        item.update(status="skipped", message="explicit, not added")
                        job["title"] = f"{t['artist']} - {t['title']}"
                        return
                    spec.update(title=t["title"], artist=t["artist"], album_artist=alb["artist"],
                                album=alb["title"], year=alb.get("year"), n=t["n"],
                                total=len(alb["tracks"]), cover=fetch_bytes(alb["thumb"]))
                    break
    except Exception as e:  # noqa: BLE001
        log.warning("album lookup failed for %s: %s", video_id, e)
    job["title"] = f"{spec.get('artist') or vd.get('author') or ''} - {spec.get('title') or vd.get('title')}"
    try:
        if download_track(spec, item) and discovery:
            store.run("INSERT OR REPLACE INTO discoveries(video_id, path, at) VALUES(?,?,?)",
                      video_id, item["path"], time.time())
    except Exception as e:  # noqa: BLE001
        item.update(status="error", message=str(e)[:300])
        if isinstance(e, BotCheck):
            job["error"] = "YouTube bot check"


def _run_urls(job, urls):
    entries = []
    for url in urls:
        # YouTube Music album playlists (list=OLAK5uy_...) -> treat as a proper album
        lst = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("list", [""])[0]
        if lst.startswith("OLAK5uy_"):
            try:
                with _ytm_lock:
                    browse = ytm().get_album_browse_id(lst)
                if browse:
                    _run_album(job, browse)
                    continue
            except Exception as e:  # noqa: BLE001
                log.warning("album resolve failed: %s", e)
        try:
            opts = _ydl_opts(TMP_DIR, download=False)
            opts.update(extract_flat="in_playlist", noplaylist=False)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info.get("entries"):
                for e in info["entries"]:
                    if e and (e.get("url") or e.get("id")):
                        entries.append((e.get("title") or e.get("id"),
                                        e.get("url") or f"https://www.youtube.com/watch?v={e['id']}"))
            else:
                entries.append((info.get("title") or url, url))
        except Exception as e:  # noqa: BLE001
            job["items"].append({"title": url, "status": "error", "message": str(e)[:300]})

    items = [{"title": t, "status": "queued", "message": "", "src": u} for t, u in entries]
    job["items"].extend(items)
    job["retry"] = {"kind": "urls"}
    for idx, ((_, url), item) in enumerate(zip(entries, items)):
        try:
            if download_track({"url": url}, item):
                _pause()
        except BotCheck as e:
            _stop_after_bot(items[idx:], str(e))
            return
        except Exception as e:  # noqa: BLE001
            item.update(status="error", message=str(e)[:300])


def _stop_after_bot(remaining, message):
    remaining[0].update(status="error", message=message)
    for it in remaining[1:]:
        if it["status"] == "queued":
            it.update(status="error", message="stopped after YouTube bot check")


def retry_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["status"] in ("queued", "running"):
        return None
    failed = [i["src"] for i in job["items"] if i["status"] == "error" and i.get("src")]
    r = job.get("retry") or {}
    if not failed or not r:
        return None
    if r["kind"] == "album":
        new = enqueue_album(r["browseId"], failed)
    elif r["kind"] == "song":
        new = enqueue_song(failed[0])
    elif r["kind"] == "replace":
        new = enqueue_replace(r["path"], failed[0])
    elif r["kind"] == "lyrics":
        new = enqueue_lyrics(failed, force=True)
    else:
        new = enqueue_urls(failed)
    with _jobs_lock:
        _jobs.pop(job_id, None)
    return new


def refresh_libraries():
    if JELLYFIN_URL and JELLYFIN_API_KEY:
        try:
            req = urllib.request.Request(f"{JELLYFIN_URL}/Library/Refresh", method="POST",
                                         headers={"X-Emby-Token": JELLYFIN_API_KEY})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("Jellyfin refresh failed: %s", e)
    navidrome.scan()


def _worker():
    while True:
        job, payload = _q.get()
        job["status"] = "running"
        try:
            if job["kind"] == "album":
                _run_album(job, payload["browseId"], payload.get("only"))
            elif job["kind"] == "song":
                _run_song(job, payload["videoId"], payload.get("clean_only"), payload.get("discovery"))
            elif job["kind"] == "replace":
                _run_replace(job, payload["path"], payload["source"])
            elif job["kind"] == "lyrics":
                _run_lyrics(job, payload["paths"], payload.get("force"))
            else:
                _run_urls(job, payload["urls"])
            failed = [i for i in job["items"] if i["status"] == "error"]
            job["status"] = "error" if failed and len(failed) == len(job["items"]) else "done"
            if any(BOT_MSG == i["message"] for i in failed):
                job["error"] = "YouTube bot check, see Settings"
            elif failed and job["status"] == "done":
                job["error"] = f"{len(failed)} track(s) failed"
            if any(i["status"] == "done" for i in job["items"]):
                refresh_libraries()
        except Exception as e:  # noqa: BLE001
            log.exception("job failed")
            job["status"] = "error"
            job["error"] = str(e)[:300]
        finally:
            _q.task_done()


def start_worker():
    threading.Thread(target=_worker, daemon=True, name="tunegrab-worker").start()
