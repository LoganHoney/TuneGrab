"""Categories: keeps Secular, Christian, Christmas and Bath Time music apart in the playlists.

Every song gets one category, worked out on its own in the background (new downloads within a few minutes):
  1. one you set yourself on the song (Library › song › Category) always wins
  2. Christmas: a Christmas word in the title, album or folder ("Silent Night", "Jingle", "Christmas Songs"…),
     or Apple Music files it under Holiday/Christmas
  3. a rule you set for the artist (Categories › artist), e.g. "everything by NF is Christian"
  4. Apple Music's genre for the song, else for the artist (iTunes Search API, free, no key): Children's Music →
     Bath Time, Christian/Gospel/CCM → Christian, anything else → Secular
The category goes into the file's Genre tag (Christian, Christmas, Bath Time; secular songs get Apple's genre, e.g.
Pop or Alternative), so Navidrome can browse and shuffle by genre too. Songs TuneGrab doesn't know (another
library in Navidrome) are sorted by their Genre tag.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import mutagen
from mutagen.id3 import ID3, TCON, ID3NoHeaderError

from . import library, store

log = logging.getLogger("tunegrab.categories")

CATEGORIES = ["Secular", "Christian", "Christmas", "Bath Time"]
DEFAULT = "Secular"
GENRE_OF = {"Christian": "Christian", "Christmas": "Christmas", "Bath Time": "Bath Time"}   # what goes in the file
ITUNES_GAP = 3.2   # seconds between Apple lookups (it allows about 20 a minute)

CHRISTMAS = re.compile(
    r"christmas|xmas|x-mas|\bsanta\b|sleigh|jingle bell|rudolph|frosty the|mistletoe|\bnoel\b|navidad|silent night|"
    r"holy night|little drummer|deck the hall|winter wonderland|let it snow|away in a manger|hark the herald|"
    r"o come,? all ye|o come,? o come|we three kings|carol of the bells|joy to the world|chestnuts roasting|"
    r"holly jolly|the twelve days|go tell it on the mountain|o little town|what child is this|feliz navidad|"
    r"it'?s beginning to look|have yourself a merry|run rudolph|blue christmas|believe \(from the polar express", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS song_tags (path TEXT PRIMARY KEY, category TEXT NOT NULL, genre TEXT, source TEXT,
  why TEXT, at REAL);
CREATE TABLE IF NOT EXISTS artist_rules (artist TEXT PRIMARY KEY, name TEXT, category TEXT NOT NULL, at REAL);
CREATE TABLE IF NOT EXISTS itunes_cache (k TEXT PRIMARY KEY, genre TEXT, at REAL);
"""

state = {"running": False, "done": 0, "todo": 0, "error": ""}
_lock = threading.Lock()
_last_call = 0.0


def _db():
    store.db().executescript(SCHEMA)


def from_genre(genre):
    """The category a Genre tag (or Apple Music genre) points to, or None for no opinion."""
    g = (genre or "").lower()
    if not g:
        return None
    if "bath time" in g or "children" in g or g in ("kids", "kid's music", "lullabies"):
        return "Bath Time"
    if "christmas" in g or "holiday" in g:
        return "Christmas"
    if any(w in g for w in ("christian", "gospel", "ccm", "worship", "praise")):
        return "Christian"
    return None


def neutral(*genres):
    """A genre for a secular song: the first that doesn't point to another category, else Pop."""
    return next((g for g in genres if g and not from_genre(g)), "Pop")


def artist_key(info):
    a = info.get("album_artist") or (info.get("artist") or "").split(",")[0]
    return a.strip().lower()


# ---------------------------------------------------------------- Apple Music genres (iTunes Search API)
class LookupDown(Exception):
    pass


def _itunes(params):
    global _last_call
    wait = _last_call + ITUNES_GAP - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode({**params, "country": "US"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TuneGrab"}), timeout=20) as r:
            return json.load(r).get("results") or []
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            raise LookupDown(f"Apple Music lookups are paused for now ({e.code})") from e
        raise
    except (urllib.error.URLError, TimeoutError) as e:
        raise LookupDown(f"Can't reach Apple Music ({e})") from e


def _cached(k, fetch):
    row = store.one("SELECT genre FROM itunes_cache WHERE k = ?", k)
    if row:
        return row["genre"] or ""
    g = fetch() or ""
    store.run("INSERT OR REPLACE INTO itunes_cache(k, genre, at) VALUES(?,?,?)", k, g, time.time())
    return g


def song_genre(info):
    artist = (info.get("artist") or "").split(",")[0].strip()
    title = re.sub(r"\s*[(\[](?:feat|ft|with)[^)\]]*[)\]]", "", info.get("title") or "").strip()
    if not artist or not title:
        return ""

    def fetch():
        hits = _itunes({"term": f"{artist} {title}", "entity": "song", "limit": 10})
        a, t = library.norm(artist), library.norm(title)
        same = [h for h in hits if a and a in library.norm(h.get("artistName"))]
        best = next((h for h in same if library.norm(h.get("trackName")) == t), None) \
            or next((h for h in same if library.norm(h.get("trackName")).startswith(t[:12])), None)
        return (best or {}).get("primaryGenreName", "")
    return _cached(f"song|{library.norm(artist)}|{library.norm(title)}", fetch)


def artist_genre(info):
    artist = (info.get("album_artist") or (info.get("artist") or "").split(",")[0]).strip()
    if not artist or artist.lower() in ("various artists", "unknown artist"):
        return ""

    def fetch():
        hits = _itunes({"term": artist, "entity": "musicArtist", "limit": 5})
        best = next((h for h in hits if library.norm(h.get("artistName")) == library.norm(artist)), None)
        return (best or {}).get("primaryGenreName", "")
    return _cached(f"artist|{library.norm(artist)}", fetch)


# ---------------------------------------------------------------- deciding and writing
def classify(info):
    """(category, genre to write, why) for one song."""
    text = " ".join([info.get("title") or "", info.get("album") or "", os.path.dirname(info.get("path") or "")])
    sg = song_genre(info)
    if CHRISTMAS.search(text) or from_genre(sg) == "Christmas":
        return "Christmas", GENRE_OF["Christmas"], "Christmas song" if CHRISTMAS.search(text) else f"Apple Music: {sg}"
    rule = store.one("SELECT category FROM artist_rules WHERE artist = ?", artist_key(info))
    if rule:
        c = rule["category"]
        return c, GENRE_OF.get(c) or neutral(sg, artist_genre(info) if from_genre(sg) else ""), "your rule for this artist"
    ag = artist_genre(info) if not from_genre(sg) else ""
    for g in (sg, ag):
        c = from_genre(g)
        if c:
            return c, GENRE_OF[c], f"Apple Music: {g}"
    return DEFAULT, sg or ag, f"Apple Music: {sg or ag}" if sg or ag else "not found on Apple Music"


def write_genre(full, genre):
    """Puts the genre in the file and nudges Navidrome to pick it up. Blank = leave the file alone."""
    if not genre:
        return
    if full.lower().endswith(".mp3"):
        try:
            tags = ID3(full)
        except ID3NoHeaderError:
            tags = ID3()
        cur = tags.getall("TCON")
        if cur and [str(x) for x in cur[0].text] == [genre]:
            return
        tags.delall("TCON")
        tags.add(TCON(encoding=3, text=genre))
        tags.save(full, v2_version=3)
    else:
        f = mutagen.File(full, easy=True)
        if f is None or (f.get("genre") or [""])[0] == genre:
            return
        f["genre"] = genre
        f.save()
    library.touch_dir(full)


def _save(path, category, genre, source, why):
    store.run("INSERT OR REPLACE INTO song_tags(path, category, genre, source, why, at) VALUES(?,?,?,?,?,?)",
              path, category, genre, source, why, time.time())


def tag_pending(on_progress=None):
    """Sorts every song that hasn't got a category yet. Returns how many files changed."""
    _db()
    known = {r["path"] for r in store.q("SELECT path FROM song_tags")}
    todo = [t for t in library.list_tracks() if t["path"] not in known]
    state.update(done=0, todo=len(todo), error="")
    changed = 0
    for t in todo:
        try:
            cat, genre, why = classify(t)
        except LookupDown as e:
            state["error"] = str(e)
            log.warning("tagging paused: %s", e)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("can't sort %s: %s", t["path"], e)
            cat, genre, why = DEFAULT, "", "lookup failed"
        try:
            write_genre(library.resolve(t["path"]), genre)
            changed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("can't write the genre of %s: %s", t["path"], e)
        _save(t["path"], cat, genre, "auto", why)
        state["done"] += 1
        if on_progress:
            on_progress(state)
    if changed:
        log.info("sorted %d songs into categories", changed)
    return changed


def start():
    """Runs tag_pending in the background (one at a time); afterwards Navidrome rescans."""
    with _lock:
        if state["running"]:
            return False
        state["running"] = True

    def run():
        from . import navidrome
        try:
            if tag_pending():
                navidrome.scan()
                store.put("last_playlists", 0)   # rebuild the mixes with the new categories soon
        except Exception as e:  # noqa: BLE001
            log.exception("tagging failed")
            state["error"] = str(e)[:300]
        finally:
            state["running"] = False
    threading.Thread(target=run, daemon=True, name="categories").start()
    return True


# ---------------------------------------------------------------- your choices
def set_song(path, category):
    if category not in CATEGORIES:
        raise ValueError("Unknown category")
    _db()
    full = library.resolve(path)
    row = store.one("SELECT genre FROM song_tags WHERE path = ?", path)
    old = (row["genre"] if row else "") or ""
    if category in GENRE_OF:
        genre = GENRE_OF[category]
    else:   # secular: its own genre from Apple Music (already looked up, so no wait)
        info = library.read_info(full)
        try:
            sg = song_genre(info)
            genre = neutral(old, sg, artist_genre(info) if from_genre(sg) or not sg else "")
        except LookupDown:
            genre = neutral(old)
    write_genre(full, genre)
    _save(path, category, genre, "manual", "you chose it")
    _rescan()


def set_artist(artist, category):
    """A rule for every song by this artist (None removes it); their songs are sorted again."""
    _db()
    key = artist.strip().lower()
    if category:
        if category not in CATEGORIES:
            raise ValueError("Unknown category")
        store.run("INSERT OR REPLACE INTO artist_rules(artist, name, category, at) VALUES(?,?,?,?)",
                  key, artist.strip(), category, time.time())
    else:
        store.run("DELETE FROM artist_rules WHERE artist = ?", key)
    tracks = [t for t in library.list_tracks() if artist_key(t) == key]
    for t in tracks:
        row = store.one("SELECT source FROM song_tags WHERE path = ?", t["path"])
        if row and row["source"] == "manual":
            continue
        cat, genre, why = classify(t)   # cached lookups: quick
        if from_genre(genre) != (None if cat == DEFAULT else cat):   # the file may hold another category's genre
            genre = GENRE_OF.get(cat) or neutral(genre)
        write_genre(library.resolve(t["path"]), genre)
        _save(t["path"], cat, genre, "auto", why)
    _rescan()
    return len(tracks)


def resort():
    """Forget the automatic choices (yours stay) and sort everything again."""
    _db()
    store.run("DELETE FROM song_tags WHERE source = 'auto'")
    return start()


def _rescan():
    from . import navidrome
    threading.Thread(target=navidrome.scan, daemon=True).start()
    store.put("last_playlists", 0)


# ---------------------------------------------------------------- reading
def by_path():
    _db()
    return {r["path"]: r["category"] for r in store.q("SELECT path, category FROM song_tags")}


def of(song, known=None):
    """The category of a Navidrome song (a dict with path and genre)."""
    known = by_path() if known is None else known
    return known.get(song.get("path")) or from_genre(song.get("genre")) or DEFAULT


def summary():
    _db()
    rows = {r["path"]: dict(r) for r in store.q("SELECT * FROM song_tags")}
    rules = {r["artist"]: r["category"] for r in store.q("SELECT artist, category FROM artist_rules")}
    tracks = library.list_tracks()
    counts = {c: 0 for c in CATEGORIES}
    artists = {}
    for t in tracks:
        r = rows.get(t["path"])
        cat = r["category"] if r else None
        if cat:
            counts[cat] += 1
        k = artist_key(t)
        a = artists.setdefault(k, {"artist": t.get("album_artist") or t.get("artist") or "Unknown",
                                   "songs": 0, "cats": {}, "rule": rules.get(k)})
        a["songs"] += 1
        if cat:
            a["cats"][cat] = a["cats"].get(cat, 0) + 1
    return {"categories": CATEGORIES, "counts": counts, "unsorted": sum(1 for t in tracks if t["path"] not in rows),
            "artists": sorted(artists.values(), key=lambda a: a["artist"].lower()),
            "state": dict(state)}


def song_info(path):
    _db()
    r = store.one("SELECT category, genre, source, why FROM song_tags WHERE path = ?", path)
    return dict(r) if r else None
