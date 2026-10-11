"""Pure helpers for special-livery expiry and retirement review candidates."""

import gzip
from datetime import date


def normalize_registration(value):
    return str(value or "").replace("-", "").replace(" ", "").upper()


def expired_liveries(liveries, today=None):
    """Return expired livery records that have a valid ISO end date."""
    today = today or date.today()
    results = []
    for registration, raw in (liveries or {}).items():
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        valid_until = str(raw.get("valid_until") or "").strip()
        if not name or not valid_until:
            continue
        try:
            end_date = date.fromisoformat(valid_until)
        except ValueError:
            continue
        if end_date < today:
            results.append({
                "registration": str(registration),
                "name": name,
                "valid_until": valid_until,
                "fingerprint": f"{normalize_registration(registration)}:{valid_until}",
            })
    return sorted(results, key=lambda item: (item["valid_until"], item["registration"]))


def registrations_in_tar1090(path, registrations):
    """Scan the compressed tar1090 database once for the requested registrations."""
    wanted = {normalize_registration(value) for value in registrations if normalize_registration(value)}
    found = set()
    if not wanted:
        return found
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            parts = line.split(";", 2)
            if len(parts) < 2:
                continue
            registration = normalize_registration(parts[1])
            if registration in wanted:
                found.add(registration)
                if found == wanted:
                    break
    return found


def update_retirement_observations(watchlist, found_registrations, state, today=None, threshold_days=30):
    """Update absence history and return conservative retirement review candidates.

    Only registrations observed in the reference database at least once can become
    candidates. This avoids treating military or otherwise unlisted aircraft as retired.
    """
    today = today or date.today()
    found = {normalize_registration(value) for value in found_registrations}
    records = dict((state or {}).get("retirement") or {})
    active_ids = set()
    candidates = []

    for icao24, raw in (watchlist or {}).items():
        entry = raw if isinstance(raw, dict) else {"label": raw}
        registration = str(entry.get("label") or "").strip()
        normalized = normalize_registration(registration)
        if not normalized or normalized == str(icao24).upper():
            continue
        key = str(icao24).lower()
        active_ids.add(key)
        record = dict(records.get(key) or {})
        record["registration"] = registration
        if normalized in found:
            record.update({
                "ever_seen": True,
                "last_seen": today.isoformat(),
                "missing_since": None,
                "next_review_on": None,
            })
        elif record.get("ever_seen"):
            missing_since = record.get("missing_since") or today.isoformat()
            record["missing_since"] = missing_since
            try:
                missing_days = (today - date.fromisoformat(missing_since)).days
            except ValueError:
                missing_since = today.isoformat()
                missing_days = 0
                record["missing_since"] = missing_since
            notified_for = record.get("notified_for")
            next_review_on = record.get("next_review_on")
            review_due = False
            if next_review_on:
                try:
                    review_due = today >= date.fromisoformat(str(next_review_on))
                except ValueError:
                    review_due = False
            if missing_days >= threshold_days and (notified_for != missing_since or review_due):
                candidates.append({
                    "icao24": key,
                    "registration": registration,
                    "type": str(entry.get("type") or "不明"),
                    "missing_since": missing_since,
                    "missing_days": missing_days,
                })
                record["notified_for"] = missing_since
                record["next_review_on"] = None
        records[key] = record

    records = {key: value for key, value in records.items() if key in active_ids}
    result = dict(state or {})
    result["retirement"] = records
    return candidates, result
