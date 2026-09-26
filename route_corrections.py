"""外部経路DBの既知の古い便名データを、期間限定で補正する。"""

from datetime import date, timezone, datetime


DL172_CORRECT_ROUTE = {
    "origin": {
        "iata_code": "ICN",
        "icao_code": "RKSI",
        "name": "Incheon International Airport",
        "municipality": "Seoul",
        "latitude": 37.4602,
        "longitude": 126.4407,
    },
    "destination": {
        "iata_code": "SLC",
        "icao_code": "KSLC",
        "name": "Salt Lake City International Airport",
        "municipality": "Salt Lake City",
        "latitude": 40.7899,
        "longitude": -111.9791,
    },
    "flight_iata": "DL172",
    "airline": "Delta Air Lines",
}


def correct_route(callsign, route, today=None):
    """DAL172の旧MNL/JFKデータを、現行ICN/SLCへ補正する。"""
    normalized = (callsign or "").strip().upper()
    if normalized != "DAL172" or not route:
        return route
    if today is None:
        today = datetime.now(timezone.utc).date()
    if not isinstance(today, date) or today > date(2027, 8, 24):
        return route
    origin = ((route.get("origin") or {}).get("iata_code") or "").upper()
    destination = ((route.get("destination") or {}).get("iata_code") or "").upper()
    if (origin, destination) == ("ICN", "SLC"):
        return route
    return {
        "origin": dict(DL172_CORRECT_ROUTE["origin"]),
        "destination": dict(DL172_CORRECT_ROUTE["destination"]),
        "flight_iata": DL172_CORRECT_ROUTE["flight_iata"],
        "airline": DL172_CORRECT_ROUTE["airline"],
    }
