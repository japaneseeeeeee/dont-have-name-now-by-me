"""Registration-based special-livery lookup shared by flight commands."""

import json
import os
from datetime import date


DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "liveries.json")


def normalize_registration(value):
    return str(value or "").replace("-", "").replace(" ", "").upper()


def load_liveries(path=DEFAULT_PATH):
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lookup_livery(registration, *, today=None, data=None):
    """Return a current livery name, or None when none is known."""
    key = normalize_registration(registration)
    if not key:
        return None

    entry = (data if data is not None else load_liveries()).get(key)
    if isinstance(entry, str):
        name = entry.strip()
        return name or None
    if not isinstance(entry, dict):
        return None

    name = str(entry.get("name") or "").strip()
    if not name:
        return None

    valid_until = entry.get("valid_until")
    if valid_until:
        try:
            if (today or date.today()) > date.fromisoformat(str(valid_until)):
                return None
        except ValueError:
            return None
    return name
