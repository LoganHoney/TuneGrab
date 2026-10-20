"""Library management: list, delete, replace audio, offload/restore files under /music."""
import logging
import os
import re
import shutil
import threading
import time

import mutagen
from mutagen.id3 import ID3, TXXX, ID3NoHeaderError

from . import store

log = logging.getLogger("tunegrab.library")

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
OFFLOAD_DIR = os.environ.get("OFFLOAD_DIR", "/offload")
BACKUP_DIR = os.path.join(DATA_DIR, "replaced")
BACKUP_DAYS = float(os.environ.get("REPLACED_KEEP_DAYS", "14"))
AUDIO_EXT = (".mp3", ".m4a", ".opus", ".ogg", ".flac")
VID_TAG = "TUNEGRAB_VIDEOID"


# ---------------------------------------------------------------- paths
def resolve(rel, base=None):
    """Map a library-relative path to an absolute one, refusing anything outside the library."""
    base = os.path.realpath(base or MUSIC_DIR)
    full = os.path.realpath(os.path.join(base, rel or ""))
    if not full.startswith(base + os.sep) or not full.lower().endswith(AUDIO_EXT):
        raise ValueError("Not a song in the library")
    return full


def relpath(full):
    return os.path.relpath(full, MUSIC_DIR)


def touch_dir(path):
    """Navidrome's quick scan only looks at folders whose mtime changed, so bump it after in-place tag edits."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    try:
        os.utime(d, None)
    except OSError:
        pass


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"\((?:feat|ft|with)[^)]*\)|\[(?:feat|ft|with)[^\]]*\]", "", s)
    s = re.sub(r"\s+-\s+(?:remaster|single|radio edit).*$", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


# ---------------------------------------------------------------- tags
def _first(f, *keys):
    for k in keys:
        v = f.get(k) if f else None
        if v:
            return str(v[0]) if isinstance(v, list) else str(v)
    return ""


def has_lyrics(full):
    if os.path.exists(os.path.splitext(full)[0] + ".lrc"):
        return True
    try:
        f = mutagen.File(full)
    except Exception:  # noqa: BLE001
        return False
    if f is None or f.tags is None:
        return False
    if full.endswith(".mp3"):
        return any(str(fr.text).strip() for fr in f.tags.getall("USLT"))
    for k in ("\xa9lyr", "lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS"):
        if f.tags.get(k):
            return True
    return False


def read_info(full):
    st = os.stat(full)
    info = {"path": relpath(full), "size": st.st_size, "mtime": st.st_mtime,
            "title": "", "artist": "", "album": "", "album_artist": "", "track": "",
            "year": "", "duration": 0, "bitrate": 0}
    try:
        easy = mutagen.File(full, easy=True)
        if easy is not None:
            info.update(title=_first(easy, "title"), artist=_first(easy, "artist"),
                        album=_first(easy, "album"), album_artist=_first(easy, "albumartist"),
                        track=_first(easy, "tracknumber"), year=_first(easy, "date")[:4])
            info["duration"] = int(getattr(easy.info, "length", 0) or 0)
            info["bitrate"] = int((getattr(easy.info, "bitrate", 0) or 0) / 1000)
    except Exception as e:  # noqa: BLE001
        log.warning("can't read tags of %s: %s", full, e)
    if not info["title"]:
        info["title"] = re.sub(r"^\d+\s*-\s*", "", os.path.splitext(os.path.basename(full))[0])
    info["lyrics"] = has_lyrics(full)
    return info


def cover_bytes(full):
    try:
        f = mutagen.File(full)
        if f is not None and f.tags is not None:
            if full.endswith(".mp3"):
                pics = f.tags.getall("APIC")
                if pics:
                    return pics[0].data, pics[0].mime
            elif f.tags.get("covr"):
                data = bytes(f.tags["covr"][0])
                return data, "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            elif getattr(f, "pictures", None):
                return f.pictures[0].data, f.pictures[0].mime
    except Exception:  # noqa: BLE001
        pass
    side = os.path.join(os.path.dirname(full), "cover.jpg")
    if os.path.exists(side):
        with open(side, "rb") as fh:
            return fh.read(), "image/jpeg"
    return None, None


def set_video_id(full, vid):
    store.run("INSERT INTO sources(path, video_id, added) VALUES(?,?,?) "
              "ON CONFLICT(path) DO UPDATE SET video_id=excluded.video_id",
              relpath(full), vid, time.time())
    if not full.endswith(".mp3"):
        return
    try:
        tags = ID3(full)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall(f"TXXX:{VID_TAG}")
    tags.add(TXXX(encoding=3, desc=VID_TAG, text=vid))
    tags.save(full, v2_version=3)


def video_id_of(rel):
    r = store.one("SELECT video_id FROM sources WHERE path=?", rel)
    return r["video_id"] if r else None


def copy_tags(old, new):
    """Move every tag (title, album, artist, cover, lyrics, ...) from the old file onto the new audio."""
    if old.endswith(".mp3") and new.endswith(".mp3"):
        try:
            tags = ID3(old)
        except ID3NoHeaderError:
            return
        try:
            ID3(new).delete(new)
        except ID3NoHeaderError:
            pass
        tags.save(new, v2_version=3)
        return
    src, dst = mutagen.File(old), mutagen.File(new)
    if src is None or dst is None or src.tags is None:
        return
    if dst.tags is None:
        dst.add_tags()
    if type(src.tags) is type(dst.tags):
        dst.tags.clear()
        dst.tags.update(src.tags)
        dst.save()
        return
    # different containers: carry the basics over through the "easy" interface
    se, de = mutagen.File(old, easy=True), mutagen.File(new, easy=True)
    for k in ("title", "artist", "albumartist", "album", "tracknumber", "date", "genre"):
        if se.get(k):
            de[k] = se[k]
    de.save()


# ---------------------------------------------------------------- listing
_cache = {}
_cache_lock = threading.Lock()


def owned_keys():
    """'title|first artist' for every song in the library, to spot downloads you already have."""
    return {norm(t["title"]) + "|" + norm(t["artist"].split(",")[0]) for t in list_tracks()}


def list_tracks():
    seen = {}
    for root, _dirs, files in os.walk(MUSIC_DIR):
        for name in files:
            if not name.lower().endswith(AUDIO_EXT) or name.startswith("."):
                continue
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            key = (st.st_mtime, st.st_size)
            with _cache_lock:
                hit = _cache.get(full)
            if not hit or hit[0] != key:
                hit = (key, read_info(full))
                with _cache_lock:
                    _cache[full] = hit
            seen[full] = hit[1]
    with _cache_lock:
        for gone in set(_cache) - set(seen):
            _cache.pop(gone, None)
    out = [dict(info, video_id=video_id_of(info["path"])) for info in seen.values()]
    out.sort(key=lambda t: (t["album_artist"].lower() or t["artist"].lower(), t["album"].lower(),
                            int(re.match(r"\d*", t["track"]).group() or 0), t["title"].lower()))
    return out


def forget(full):
    with _cache_lock:
        _cache.pop(full, None)


# ---------------------------------------------------------------- delete / offload
def _prune_dirs(folder, base):
    """Remove an album folder (and then the artist folder) once no audio is left in it."""
    base = os.path.realpath(base)
    while os.path.realpath(folder).startswith(base + os.sep):
        left = os.listdir(folder)
        if any(n.lower().endswith(AUDIO_EXT) for n in left) or any(
                os.path.isdir(os.path.join(folder, n)) for n in left):
            return
        for n in left:  # cover.jpg, .lrc leftovers, etc.
            os.remove(os.path.join(folder, n))
        os.rmdir(folder)
        folder = os.path.dirname(folder)


def delete(rel):
    full = resolve(rel)
    if not os.path.exists(full):
        raise FileNotFoundError(rel)
    vid = video_id_of(rel)
    os.remove(full)
    lrc = os.path.splitext(full)[0] + ".lrc"
    if os.path.exists(lrc):
        os.remove(lrc)
    forget(full)
    folder = os.path.dirname(full)
    _prune_dirs(folder, MUSIC_DIR)
    touch_dir(folder if os.path.isdir(folder) else MUSIC_DIR)
    for table in ("sources", "lyrics_misses", "skip_log", "cleanup_keep"):
        store.run(f"DELETE FROM {table} WHERE path=?", rel)
    return vid




def _move(src, dst):
    """Move a song (plus its .lrc, and a copy of the folder's cover.jpg) to another tree."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    lrc = os.path.splitext(src)[0] + ".lrc"
    if os.path.exists(lrc):
        shutil.move(lrc, os.path.splitext(dst)[0] + ".lrc")
    cover = os.path.join(os.path.dirname(src), "cover.jpg")
    dcover = os.path.join(os.path.dirname(dst), "cover.jpg")
    if os.path.exists(cover) and not os.path.exists(dcover):
        shutil.copy2(cover, dcover)


def offload_available():
    return os.path.isdir(OFFLOAD_DIR) and os.access(OFFLOAD_DIR, os.W_OK)


def offload(rel):
    """Move a song out of the library into the offload folder, keeping its Artist/Album layout."""
    if not offload_available():
        raise RuntimeError(f"Offload folder {OFFLOAD_DIR} isn't mounted or writable")
    full = resolve(rel)
    if not os.path.exists(full):
        raise FileNotFoundError(rel)
    info = read_info(full)
    dst = os.path.join(OFFLOAD_DIR, rel)
    if os.path.exists(dst):
        raise FileExistsError("A song with that name is already in the offload folder")
    _move(full, dst)
    forget(full)
    folder = os.path.dirname(full)
    _prune_dirs(folder, MUSIC_DIR)
    touch_dir(folder if os.path.isdir(folder) else MUSIC_DIR)
    store.run("INSERT OR REPLACE INTO offloaded(path, title, artist, album, at) VALUES(?,?,?,?,?)",
              rel, info["title"], info["artist"], info["album"], time.time())


def restore(rel):
    src = resolve(rel, OFFLOAD_DIR)
    dst = os.path.join(MUSIC_DIR, rel)
    if not os.path.exists(src):
        raise FileNotFoundError("That song is no longer in the offload folder")
    if os.path.exists(dst):
        raise FileExistsError("A song with that name is already back in the library")
    _move(src, dst)
    _prune_dirs(os.path.dirname(src), OFFLOAD_DIR)
    touch_dir(dst)
    store.run("DELETE FROM offloaded WHERE path=?", rel)


def list_offloaded():
    """Offloaded songs, including any files someone dropped into the folder by hand."""
    rows = {r["path"]: r for r in store.q("SELECT * FROM offloaded")}
    out = []
    if offload_available():
        for root, _d, files in os.walk(OFFLOAD_DIR):
            for n in files:
                if n.lower().endswith(AUDIO_EXT) and not n.startswith("."):
                    rel = os.path.relpath(os.path.join(root, n), OFFLOAD_DIR)
                    r = rows.get(rel) or {"path": rel, "title": os.path.splitext(n)[0], "artist": "",
                                          "album": "", "at": None}
                    out.append(r)
    return sorted(out, key=lambda r: -(r["at"] or 0))


# ---------------------------------------------------------------- replace
def backup_for_replace(full):
    """Keep the previous audio for a while so a bad replacement can be undone."""
    rel = relpath(full)
    dst = os.path.join(BACKUP_DIR, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(full, dst)  # fresh mtime, so purge_backups counts from the replacement
    return dst


def backup_of(rel):
    p = os.path.join(BACKUP_DIR, rel)
    return p if os.path.exists(p) else None


def undo_replace(rel):
    full = resolve(rel)
    b = backup_of(rel)
    if not b:
        raise FileNotFoundError("No earlier version kept for this song")
    shutil.move(b, full)
    forget(full)
    touch_dir(full)
    # the old file carries its own source tag; point our records back at it
    vid = None
    if full.endswith(".mp3"):
        try:
            frames = ID3(full).getall(f"TXXX:{VID_TAG}")
            vid = str(frames[0].text[0]) if frames else None
        except ID3NoHeaderError:
            pass
    if vid:
        store.run("UPDATE sources SET video_id=? WHERE path=?", vid, rel)
    else:
        store.run("DELETE FROM sources WHERE path=?", rel)
    return vid


def purge_backups():
    cutoff = time.time() - BACKUP_DAYS * 86400
    for root, _d, files in os.walk(BACKUP_DIR):
        for n in files:
            p = os.path.join(root, n)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
