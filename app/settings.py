"""Settings people can change on the Settings page. Each one starts from its .env value (or the default);
a change made in the app is saved in the database and takes priority from then on."""
import json
import os
import re

from . import store

VERSIONS = {  # "hide these versions" choices and how each shows up in a title
    "live": r"\blive\b",
    "remix": r"\bremix(ed)?\b",
    "acoustic": r"\bacoustic\b",
    "instrumental": r"\binstrumental\b",
    "karaoke": r"\bkaraoke\b",
    "sped up / slowed": r"\bsped[ -]?up\b|\bslowed\b|\bnightcore\b",
    "cover": r"\bcover\b|\btribute\b",
}

FIELDS = {
    "max_suggestions": {"type": "int", "env": "MAX_SUGGESTIONS", "default": 25, "min": 0, "max": 100},
    "auto_add": {"type": "int", "env": "AUTO_ADD_SUGGESTIONS", "default": 3, "min": 0, "max": 25},
    "audio_format": {"type": "choice", "env": "AUDIO_FORMAT", "default": "mp3", "choices": ["mp3", "m4a", "opus"]},
    "lyrics_auto": {"type": "bool", "env": "LYRICS_AUTO", "default": True},
    "clean_only": {"type": "bool", "env": "CLEAN_ONLY", "default": False},
    "hide_versions": {"type": "list", "env": "HIDE_VERSIONS", "default": [], "choices": list(VERSIONS)},
    "playlist_size": {"type": "int", "env": "PLAYLIST_SIZE", "default": 50, "min": 10, "max": 200},
}


def _parse(f, v):
    if f["type"] == "int":
        return max(f["min"], min(f["max"], int(v)))
    if f["type"] == "bool":
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
    if f["type"] == "choice":
        v = str(v).strip().lower()
        if v not in f["choices"]:
            raise ValueError(f"must be one of {', '.join(f['choices'])}")
        return v
    items = v if isinstance(v, list) else [x.strip() for x in str(v).split(",")]
    return [x for x in f["choices"] if x in [str(i).strip().lower() for i in items]]


def _defaults():
    out = {}
    for name, f in FIELDS.items():
        try:
            out[name] = _parse(f, os.environ[f["env"]]) if os.environ.get(f["env"], "").strip() else f["default"]
        except ValueError:
            out[name] = f["default"]
    return out


def current():
    saved = json.loads(store.get("settings") or "{}")
    return {**_defaults(), **{k: v for k, v in saved.items() if k in FIELDS}}


def get(name):
    return current()[name]


def update(changes):
    saved = json.loads(store.get("settings") or "{}")
    for name, value in changes.items():
        if name not in FIELDS:
            raise ValueError(f"Unknown setting {name}")
        try:
            saved[name] = _parse(FIELDS[name], value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{name}: {e}") from e
    store.put("settings", json.dumps(saved))
    return current()


def unwanted_version(title, artist=""):
    """True if the title is a version the user chose to hide. Only the label part counts, the bit in
    brackets or after a dash ("Song (Live at Wembley)", "Song - Acoustic"), so "Live Forever" stays.
    Karaoke and tribute acts often put it in the artist name instead."""
    pats = [VERSIONS[v] for v in get("hide_versions")]
    if not pats:
        return False
    title = title or ""
    label = " ".join(re.findall(r"[(\[]([^)\]]*)[)\]]", title) + re.findall(r"\s[-\u2013]\s(.*)$", title))
    return re.search("|".join(pats), f"{label} {artist or ''}", re.I) is not None
