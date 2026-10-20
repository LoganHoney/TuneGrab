"""Lyrics lookup (LRCLIB first, YouTube Music as fallback) and embedding into audio files."""
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import mutagen
from mutagen.id3 import ID3, USLT, ID3NoHeaderError

from . import library

log = logging.getLogger("tunegrab.lyrics")

LRCLIB = os.environ.get("LRCLIB_URL", "https://lrclib.net").rstrip("/")
UA = "TuneGrab/1.1 (self-hosted music downloader)"


def _get(path, params):
    url = f"{LRCLIB}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _pick(rec):
    if not rec or rec.get("instrumental"):
        return None
    if rec.get("syncedLyrics"):
        return rec["syncedLyrics"].strip(), True
    if rec.get("plainLyrics"):
        return rec["plainLyrics"].strip(), False
    return None


def from_lrclib(artist, title, album="", duration=0):
    if album and duration:
        hit = _pick(_get("/api/get", {"artist_name": artist, "track_name": title,
                                      "album_name": album, "duration": int(duration)}))
        if hit:
            return hit
    results = _get("/api/search", {"artist_name": artist, "track_name": title}) or []
    # closest length wins; a different edit of the song would put synced lines out of step
    results = [r for r in results if not duration or abs((r.get("duration") or 0) - duration) <= 4]
    results.sort(key=lambda r: (not r.get("syncedLyrics"), abs((r.get("duration") or 0) - (duration or 0))))
    for r in results:
        hit = _pick(r)
        if hit:
            return hit
    return None


def _lrc_time(ms):
    return f"[{ms // 60000:02d}:{ms % 60000 / 1000:05.2f}]"


def from_ytmusic(artist, title, video_id=None):
    from . import downloader as dl  # shares the YouTube Music session and its lock
    with dl._ytm_lock:
        if not video_id:
            hits = dl.ytm().search(f"{artist} {title}", filter="songs", limit=5)
            want = library.norm(title)
            hits = [h for h in hits if library.norm(h.get("title")) == want]
            if not hits:
                return None
            video_id = hits[0]["videoId"]
        browse = dl.ytm().get_watch_playlist(videoId=video_id, limit=1).get("lyrics")
        if not browse:
            return None
        lyr = dl.ytm().get_lyrics(browse, timestamps=True)
    if not lyr or not lyr.get("lyrics"):
        return None
    if lyr.get("hasTimestamps"):
        lines = [f"{_lrc_time(ln.start_time)}{ln.text}" for ln in lyr["lyrics"]]
        return "\n".join(lines), True
    return str(lyr["lyrics"]).strip(), False


class LrclibDown(RuntimeError):
    """LRCLIB couldn't be asked, so "nothing found" isn't known yet; the song is tried again later."""


def find(artist, title, album="", duration=0, video_id=None):
    """Returns (text, synced, source) or None. Raises LrclibDown rather than settle for plain lyrics
    (or none) while LRCLIB, the main source of synced lyrics, is unreachable."""
    lrclib_down = False
    for source, fn in (("LRCLIB", lambda: from_lrclib(artist, title, album, duration)),
                       ("YouTube Music", lambda: from_ytmusic(artist, title, video_id))):
        try:
            hit = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("%s lyrics lookup failed for %s - %s: %s", source, artist, title, e)
            lrclib_down = lrclib_down or source == "LRCLIB"
            continue
        if hit and (hit[1] or not lrclib_down):
            return hit[0], hit[1], source
    if lrclib_down:
        raise LrclibDown("LRCLIB unavailable, will try again later")
    return None


def embed(full, text):
    """Store lyrics in the file itself. Navidrome reads embedded LRC text as synced lyrics."""
    if full.endswith(".mp3"):
        try:
            tags = ID3(full)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
        tags.save(full, v2_version=3)
    else:
        f = mutagen.File(full)
        if f is None:
            raise RuntimeError("unsupported file type")
        if f.tags is None:
            f.add_tags()
        f.tags["\xa9lyr" if full.endswith(".m4a") else "LYRICS"] = text
        f.save()
    library.forget(full)
    library.touch_dir(full)


def read(full):
    lrc = os.path.splitext(full)[0] + ".lrc"
    if os.path.exists(lrc):
        with open(lrc, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    f = mutagen.File(full)
    if f is None or f.tags is None:
        return ""
    if full.endswith(".mp3"):
        fr = f.tags.getall("USLT")
        return str(fr[0].text) if fr else ""
    for k in ("\xa9lyr", "lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS"):
        if f.tags.get(k):
            v = f.tags[k]
            return str(v[0] if isinstance(v, list) else v)
    return ""


_STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def shift(full, seconds):
    """Move every synced line by `seconds` (positive = later), wherever the lyrics are stored."""
    text = read(full)
    if not _STAMP.search(text):
        raise ValueError("These lyrics aren't synced, so there's no timing to shift")

    def move(m):
        ms = round((int(m.group(1)) * 60 + float(m.group(2)) + seconds) * 1000)
        return _lrc_time(max(ms, 0))

    text = _STAMP.sub(move, text)
    lrc = os.path.splitext(full)[0] + ".lrc"
    if os.path.exists(lrc):
        with open(lrc, "w", encoding="utf-8") as fh:
            fh.write(text)
        library.touch_dir(full)
    else:
        embed(full, text)
    return text


def fetch_for(full, video_id=None):
    """Look up and embed lyrics for one library file. Returns the source name, or None if nothing found."""
    info = library.read_info(full)
    hit = find(info["artist"].split(",")[0].strip() or info["album_artist"], info["title"],
               info["album"], info["duration"], video_id or library.video_id_of(info["path"]))
    if not hit:
        return None
    embed(full, hit[0])
    return f"{hit[2]}{' (synced)' if hit[1] else ''}"
