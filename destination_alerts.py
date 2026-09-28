"""指定機体が指定空港へ向かう便を検出する早期通知ルール。"""
import hashlib
import re
import time

MAX_RULES = 50
SUPPRESS_SECONDS = 20 * 60 * 60
AIRPORT_RE = re.compile(r"^[A-Z0-9]{3,4}$")

def empty_store():
    return {"next_id": 1, "rules": [], "notified": {}}

def normalize_airport(value):
    code = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return code if AIRPORT_RE.fullmatch(code) else ""

def add_rule(store, *, registration, icao24, aircraft_type, destination, owner_id):
    destination = normalize_airport(destination)
    if not destination:
        return None, False
    normalized_icao = str(icao24 or "").strip().lower()
    for rule in store.setdefault("rules", []):
        if rule.get("icao24") == normalized_icao and rule.get("destination") == destination:
            return rule, False
    rule = {"id": int(store.get("next_id", 1)), "registration": str(registration or normalized_icao).upper(), "icao24": normalized_icao, "type": aircraft_type or "不明", "destination": destination, "owner_id": str(owner_id), "created_at": time.time()}
    store["next_id"] = rule["id"] + 1
    store["rules"].append(rule)
    return rule, True

def airport_codes(airport):
    if not isinstance(airport, dict):
        return set()
    return {str(airport.get(key) or "").strip().upper() for key in ("iata_code", "icao_code") if airport.get(key)}

def route_matches_destination(route, destination):
    return normalize_airport(destination) in airport_codes((route or {}).get("destination"))

def event_fingerprint(rule, aircraft, route):
    callsign = str(aircraft.get("flight") or "").strip().upper()
    origin = "/".join(sorted(airport_codes(route.get("origin"))))
    destination = "/".join(sorted(airport_codes(route.get("destination"))))
    raw = f"{rule['icao24']}|{callsign}|{origin}|{destination}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def should_notify(notified, fingerprint, now=None):
    now = time.time() if now is None else now
    try:
        last = float(notified.get(fingerprint, 0))
    except (TypeError, ValueError):
        last = 0
    return not last or now - last >= SUPPRESS_SECONDS

def prune_notified(notified, now=None):
    now = time.time() if now is None else now
    cutoff = now - 3 * 86400
    cleaned = {}
    for key, stamp in (notified or {}).items():
        try:
            if float(stamp) >= cutoff:
                cleaned[key] = float(stamp)
        except (TypeError, ValueError):
            continue
    return cleaned
