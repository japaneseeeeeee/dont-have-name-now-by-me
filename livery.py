"""Registration-based special-livery lookup shared by flight commands."""

import json
import os
from datetime import date


DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "liveries.json")
_CACHE = {}


def normalize_registration(value):
    return str(value or "").replace("-", "").replace(" ", "").upper()


def load_liveries(path=DEFAULT_PATH):
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    cached = _CACHE.get(os.path.abspath(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    result = data if isinstance(data, dict) else {}
    _CACHE[os.path.abspath(path)] = (mtime, result)
    return result


def lookup_livery(registration, *fallback_identifiers, today=None, data=None):
    """Return a current livery name for the first known identifier.

    通常は登録記号で引くが、通知データの表記揺れや登録記号欠落時に備えて
    icao24などの代替識別子も順に確認できるようにする。
    """
    liveries = data if data is not None else load_liveries()
    for identifier in (registration, *fallback_identifiers):
        key = normalize_registration(identifier)
        if not key:
            continue
        entry = liveries.get(key)
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                return name
            continue
        if not isinstance(entry, dict):
            continue

        name = str(entry.get("name") or "").strip()
        if not name:
            continue

        valid_until = entry.get("valid_until")
        if valid_until:
            try:
                if (today or date.today()) > date.fromisoformat(str(valid_until)):
                    continue
            except ValueError:
                continue
        return name
    return None
