"""便名と実際の機材を照合する通知ルール用ヘルパー。"""

import re


TYPE_ALIASES = {
    "77W": "B77W",
    "B773ER": "B77W",
    "777300ER": "B77W",
    "359": "A359",
    "350900": "A359",
    "A350900": "A359",
    "789": "B789",
    "7879": "B789",
    "B7879": "B789",
    "788": "B788",
    "7878": "B788",
    "B7878": "B788",
    "388": "A388",
    "A380": "A388",
    "A380800": "A388",
}


def compact(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalize_equipment(value):
    key = compact(value)
    return TYPE_ALIASES.get(key, key)


def normalize_flight(value):
    return compact(value)


def aircraft_matches(aircraft, requested):
    wanted = normalize_equipment(requested)
    if not wanted:
        return False
    candidates = {
        normalize_equipment(aircraft.get("t")),
        normalize_equipment(aircraft.get("desc")),
    }
    description = compact(aircraft.get("desc"))
    if wanted == "B77W" and "777300ER" in description:
        candidates.add("B77W")
    return wanted in candidates


def empty_store():
    return {"next_id": 1, "rules": [], "notified": {}}


def add_rule(store, *, owner_id, scope, flight, equipment):
    flight = normalize_flight(flight)
    equipment = normalize_equipment(equipment)
    for rule in store["rules"]:
        if (
            str(rule["owner_id"]) == str(owner_id)
            and rule["scope"] == scope
            and rule["flight"] == flight
            and rule["equipment"] == equipment
        ):
            return rule, False
    rule = {
        "id": int(store.get("next_id", 1)),
        "owner_id": str(owner_id),
        "scope": scope,
        "flight": flight,
        "equipment": equipment,
    }
    store["next_id"] = rule["id"] + 1
    store["rules"].append(rule)
    return rule, True
