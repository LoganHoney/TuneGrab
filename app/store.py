"""Small SQLite store for state that has to survive restarts (/data/tunegrab.db)."""
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "tunegrab.db")

_lock = threading.RLock()
_conn = None

SCHEMA = """
-- which YouTube video each library file came from (relpath under /music)
CREATE TABLE IF NOT EXISTS sources (
  path TEXT PRIMARY KEY, video_id TEXT NOT NULL, added REAL
);
CREATE INDEX IF NOT EXISTS sources_vid ON sources(video_id);

-- songs you moved out of the library (files live in OFFLOAD_DIR under the same relpath)
CREATE TABLE IF NOT EXISTS offloaded (path TEXT PRIMARY KEY, title TEXT, artist TEXT, album TEXT, at REAL);

-- one row per skip seen in Navidrome's now-playing feed (path = library relpath)
CREATE TABLE IF NOT EXISTS skip_log (
  id INTEGER PRIMARY KEY, path TEXT, song_id TEXT, title TEXT, artist TEXT, listened REAL, at REAL
);
CREATE INDEX IF NOT EXISTS skip_log_path ON skip_log(path);

-- songs you chose to keep, hidden from the cleanup lists
CREATE TABLE IF NOT EXISTS cleanup_keep (path TEXT PRIMARY KEY, at REAL);

-- lyrics lookups that found nothing, so the bulk job doesn't hammer the same songs
CREATE TABLE IF NOT EXISTS lyrics_misses (path TEXT PRIMARY KEY, at REAL);

CREATE TABLE IF NOT EXISTS suggestions (
  video_id TEXT PRIMARY KEY, title TEXT, artist TEXT, album TEXT, thumb TEXT,
  duration TEXT, reason TEXT, score REAL, status TEXT DEFAULT 'new', at REAL
);

-- songs TuneGrab added on its own from suggestions (feeds the Discoveries playlist)
CREATE TABLE IF NOT EXISTS discoveries (video_id TEXT PRIMARY KEY, path TEXT, at REAL);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(DATA_DIR, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def q(sql, *args):
    with _lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def one(sql, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


def run(sql, *args):
    with _lock:
        db().execute(sql, args)


def get(k, default=None):
    r = one("SELECT v FROM kv WHERE k=?", k)
    return r["v"] if r else default


def put(k, v):
    run("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, str(v))


def now():
    return time.time()
