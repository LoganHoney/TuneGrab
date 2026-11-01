"""Navidrome (Subsonic API): rescans, listening history, skip log, curated playlists, cleanup lists."""
import datetime as dt
import hashlib
import json
import logging
import os
import random
import secrets
import threading
import time
import urllib.parse
import urllib.request

from . import settings, store

log = logging.getLogger("tunegrab.navidrome")

URL = os.environ.get("NAVIDROME_URL", "").rstrip("/")
USER = os.environ.get("NAVIDROME_USER", "")
PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "")
PLAYLIST_PREFIX = os.environ.get("PLAYLIST_PREFIX", "TuneGrab: ")

# cleanup lists (reviewed with the playlists); nothing is ever offloaded automatically
SKIP_MIN = int(os.environ.get("SKIP_MIN", "3"))  # skips in the last SKIP_WINDOW_DAYS
SKIP_RATIO = float(os.environ.get("SKIP_RATIO", "0.6"))  # skips / (skips + plays)
SKIP_WINDOW_DAYS = int(os.environ.get("SKIP_WINDOW_DAYS", "90"))
RARELY_PLAYED_DAYS = int(os.environ.get("RARELY_PLAYED_DAYS", "60"))
SKIP_LOG = os.environ.get("SKIP_LOG", "true").lower() == "true"

PLAYLIST_NOTE = "Built by TuneGrab from your listening. It is rebuilt automatically, so manual edits get replaced."


def enabled():
    return bool(URL and USER and PASSWORD)


class NavidromeError(Exception):
    pass


def call(endpoint, method="GET", **params):
    salt = secrets.token_hex(6)
    token = hashlib.md5((PASSWORD + salt).encode()).hexdigest()
    base = {"u": USER, "t": token, "s": salt, "v": "1.16.1", "c": "tunegrab", "f": "json"}
    body = urllib.parse.urlencode({**base, **params}, doseq=True)
    if method == "POST":  # formPost: long song lists don't fit in a URL
        req = urllib.request.Request(f"{URL}/rest/{endpoint}", data=body.encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        req = urllib.request.Request(f"{URL}/rest/{endpoint}?{body}")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)["subsonic-response"]
    if resp.get("status") != "ok":
        raise NavidromeError((resp.get("error") or {}).get("message", "Navidrome error"))
    return resp


def status():
    if not enabled():
        return {"configured": False}
    try:
        r = call("ping")
        return {"configured": True, "ok": True, "user": USER, "version": r.get("serverVersion")}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "ok": False, "user": USER, "error": str(e)}


def scan():
    if not enabled():
        return
    try:
        call("startScan")
    except Exception as e:  # noqa: BLE001
        log.warning("Navidrome scan failed: %s", e)


def all_songs():
    songs, offset = [], 0
    while True:
        r = call("search3", query="", songCount=500, songOffset=offset, artistCount=0, albumCount=0)
        batch = (r.get("searchResult3") or {}).get("song") or []
        songs.extend(batch)
        if len(batch) < 500:
            return songs
        offset += 500


def _ts(iso):
    if not iso:
        return 0
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


# ---------------------------------------------------------------- skip log
class SkipLog:
    """Watches getNowPlaying (a ~150 byte local request). Navidrome only remembers finished plays, so
    a skip has to be caught as it happens: the player moved on before the song was scrobbled and
    before its halfway point. Skips are only logged here; the review runs with the playlists."""

    def __init__(self):
        self.players = {}
        self.lock = threading.Lock()

    def poll(self):
        entries = (call("getNowPlaying").get("nowPlaying") or {}).get("entry") or []
        # only NAVIDROME_USER's listening feeds the Cleanup list; other accounts' skips aren't yours
        entries = [e for e in entries if (e.get("username") or "").lower() == USER.lower()]
        now = time.time()
        current = {(e.get("username"), e.get("playerId") or e.get("playerName")): e for e in entries}
        with self.lock:
            for key, e in current.items():
                prev = self.players.get(key)
                if prev and prev["id"] == e["id"]:
                    continue
                if prev:
                    self._finish(prev, now)
                self.players[key] = {"id": e["id"], "path": e.get("path"), "title": e.get("title"),
                                     "artist": e.get("artist"), "duration": e.get("duration") or 0,
                                     "base": e.get("playCount") or 0, "seen": now}
            # player went idle: can't tell a skip from "stopped listening", so don't count it
            for key in set(self.players) - set(current):
                self.players.pop(key)

    def _finish(self, prev, now):
        listened = now - prev["seen"]
        if not prev.get("path") or (prev["duration"] and listened >= min(prev["duration"] * 0.5, 240)):
            return
        try:
            count = call("getSong", id=prev["id"])["song"].get("playCount") or 0
        except Exception:  # noqa: BLE001
            return
        if count > prev["base"]:
            return  # Navidrome counted it as a play
        store.run("INSERT INTO skip_log(path, song_id, title, artist, listened, at) VALUES(?,?,?,?,?,?)",
                  prev["path"], prev["id"], prev["title"], prev["artist"], round(listened), now)
        log.info("skip: %s - %s after %ds", prev["artist"], prev["title"], listened)


skip_log = SkipLog()


def build_cleanup(songs):
    """Two review lists, refreshed every few hours: songs you skip a lot, and songs you never reach for."""
    now = time.time()
    since = now - SKIP_WINDOW_DAYS * 86400
    store.run("DELETE FROM skip_log WHERE at < ?", now - 365 * 86400)
    kept = {r["path"] for r in store.q("SELECT path FROM cleanup_keep")}
    by_path = {s.get("path"): s for s in songs}
    skipped = []
    for r in store.q("SELECT path, count(*) AS skips, max(at) AS last_skip, avg(listened) AS avg_listened "
                     "FROM skip_log WHERE at >= ? GROUP BY path", since):
        s = by_path.get(r["path"])
        if not s or r["path"] in kept:
            continue
        plays = s.get("playCount") or 0
        if r["skips"] >= SKIP_MIN and r["skips"] / (r["skips"] + plays) >= SKIP_RATIO:
            skipped.append({"path": r["path"], "title": s.get("title"), "artist": s.get("artist"),
                            "album": s.get("album"), "skips": r["skips"], "plays": plays,
                            "avg_listened": round(r["avg_listened"] or 0), "last_skip": r["last_skip"]})
    skipped.sort(key=lambda x: (-x["skips"], x["plays"]))

    rare = []
    for s in songs:
        if (s.get("path") in kept or s.get("starred") or (s.get("userRating") or 0) >= 4
                or now - _ts(s.get("created")) < RARELY_PLAYED_DAYS * 86400):
            continue
        if (s.get("playCount") or 0) <= 1 and now - _ts(s.get("played")) > RARELY_PLAYED_DAYS * 86400:
            rare.append({"path": s["path"], "title": s.get("title"), "artist": s.get("artist"),
                         "album": s.get("album"), "plays": s.get("playCount") or 0,
                         "last_played": _ts(s.get("played")) or None, "added": _ts(s.get("created"))})
    rare.sort(key=lambda x: (x["plays"], x["added"]))
    report = {"at": now, "skipped": skipped, "rare": rare,
              "rules": {"skip_min": SKIP_MIN, "skip_ratio": SKIP_RATIO, "window_days": SKIP_WINDOW_DAYS,
                        "rare_days": RARELY_PLAYED_DAYS, "skip_log": SKIP_LOG}}
    store.put("cleanup", json.dumps(report))
    return report


def drop_from_cleanup(path):
    """Take a song off the lists straight away (kept, offloaded or deleted) instead of at the next review."""
    report = cleanup_status()
    if report:
        for k in ("skipped", "rare"):
            report[k] = [s for s in report[k] if s["path"] != path]
        store.put("cleanup", json.dumps(report))


def cleanup_status():
    raw = store.get("cleanup")
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------- playlists
def _spread(songs):
    """Shuffle-ish order that avoids the same artist twice in a row where possible."""
    out, pool = [], list(songs)
    while pool:
        last = out[-1].get("artist") if out else None
        pick = next((s for s in pool if s.get("artist") != last), pool[0])
        pool.remove(pick)
        out.append(pick)
    return out


# the mixes for each category (categories.py): secular music keeps the original names, so the playlists you
# already have stay the same; nothing ever crosses between categories
MIXES = {
    "Secular": ["Daily Mix", "Heavy Rotation", "Rediscover", "Fresh Additions", "Never Played", "Discoveries"],
    "Christian": ["Daily Mix", "Heavy Rotation", "Fresh Additions"],
    "Christmas": ["All"],
    "Bath Time": ["All"],
}
ALL_MAX = 300   # an "All" playlist: every song in the category, shuffled each day


def _mixes(pool, kinds, rng, n, discovered):
    now = time.time()
    played = [s for s in pool if (s.get("playCount") or 0) > 0]
    never = [s for s in pool if not s.get("playCount")]
    rng.shuffle(never)
    out = {}
    if "Daily Mix" in kinds:
        # mostly favourites (weighted by plays, stars and ratings), topped up with unplayed songs
        weights = [(s.get("playCount") or 0) + (5 if s.get("starred") else 0) + (s.get("userRating") or 0) for s in played]
        favs = []
        cand = list(zip(played, weights))
        while cand and len(favs) < int(n * 0.7):
            pick = rng.choices(range(len(cand)), weights=[w + 0.5 for _, w in cand])[0]
            favs.append(cand.pop(pick)[0])
        daily = favs + never[: n - len(favs)]
        rng.shuffle(daily)
        out["Daily Mix"] = _spread(daily)
    if "Heavy Rotation" in kinds:
        out["Heavy Rotation"] = sorted([s for s in played if s["playCount"] >= 2 or s.get("starred")],
                                       key=lambda s: (-(s.get("playCount") or 0), -_ts(s.get("played"))))[:n]
    if "Rediscover" in kinds:
        out["Rediscover"] = sorted([s for s in played if now - _ts(s.get("played")) > 30 * 86400],
                                   key=lambda s: -(s.get("playCount") or 0))[:n]
    if "Fresh Additions" in kinds:
        out["Fresh Additions"] = sorted([s for s in pool if now - _ts(s.get("created")) < 30 * 86400],
                                        key=lambda s: -_ts(s.get("created")))[:n]
    if "Never Played" in kinds:
        out["Never Played"] = never[:n]
    if "Discoveries" in kinds:
        by_path = {s.get("path"): s for s in pool}
        out["Discoveries"] = [by_path[p] for p in discovered if p in by_path][:n]
    if "All" in kinds:
        everything = list(pool)
        rng.shuffle(everything)
        out["All"] = _spread(everything[:ALL_MAX])
    return out


def _name(cat, kind):
    if cat == "Secular":
        return kind
    return cat if kind == "All" else f"{cat} {kind}"


def build_playlists():
    from . import categories
    songs = all_songs()
    build_cleanup(songs)
    now = time.time()
    rng = random.Random(dt.date.today().isoformat())  # same picks all day, fresh ones tomorrow
    n = settings.get("playlist_size")
    known = categories.by_path()
    by_cat = {c: [] for c in MIXES}
    for s in songs:
        by_cat.setdefault(categories.of(s, known), []).append(s)
    discovered = [r["path"] for r in store.q("SELECT path FROM discoveries ORDER BY at DESC")]
    wanted = {}
    for cat, kinds in MIXES.items():
        for kind, tracks in _mixes(by_cat.get(cat) or [], kinds, rng, n, discovered).items():
            wanted[_name(cat, kind)] = tracks
    existing = {p["name"]: p for p in (call("getPlaylists").get("playlists") or {}).get("playlist") or []
                if p.get("owner") == USER}
    result = []
    for name, tracks in wanted.items():
        full = PLAYLIST_PREFIX + name
        ids = [s["id"] for s in tracks]
        pl = existing.get(full)
        if not pl and len(ids) < 3:
            result.append({"name": full, "songs": 0, "skipped": "not enough songs yet"})
            continue
        if pl:
            call("createPlaylist", method="POST", playlistId=pl["id"], songId=ids)
        else:
            pl = call("createPlaylist", method="POST", name=full, songId=ids)["playlist"]
        # public so the other Navidrome accounts in the house can play them too
        call("updatePlaylist", playlistId=pl["id"], comment=PLAYLIST_NOTE, public="true")
        result.append({"name": full, "songs": len(ids), "id": pl["id"]})
    store.put("playlists", json.dumps({"at": now, "lists": result}))
    log.info("playlists rebuilt: %s", ", ".join(f"{r['name']} ({r['songs']})" for r in result))
    return result


def playlists_status():
    raw = store.get("playlists")
    return json.loads(raw) if raw else {"at": None, "lists": []}


def top_seeds(limit=6):
    """Most-loved songs to base suggestions on."""
    songs = all_songs()
    now = time.time()

    def score(s):
        recent = 1.5 if now - _ts(s.get("played")) < 14 * 86400 else 1.0
        return ((s.get("playCount") or 0) + (4 if s.get("starred") else 0) + (s.get("userRating") or 0)) * recent

    ranked = sorted(songs, key=score, reverse=True)
    liked = [s for s in ranked if score(s) > 0]
    seeds = liked[:limit]
    # spread seeds across artists so one band doesn't drown out the rest
    by_artist = {}
    for s in liked:
        by_artist.setdefault(s.get("artist"), s)
    for s in by_artist.values():
        if len(seeds) >= limit + 3:
            break
        if s not in seeds:
            seeds.append(s)
    return seeds or random.sample(songs, min(limit, len(songs)))
