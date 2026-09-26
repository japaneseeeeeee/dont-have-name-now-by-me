import os
import sys
import json
import gzip
import urllib.request
import urllib.error
import urllib.parse

from livery import lookup_livery

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

API = "https://api.github.com"
TAR1090_URL = "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"


def request(url, method="GET", data=None, headers=None):
    h = {"User-Agent": "aircraft-alert-action/1.0"}
    if headers:
        h.update(headers)

    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def discord_send(channel_id, text):
    request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        method="POST",
        data={
            "content": text[:2000],
            "allowed_mentions": {"parse": []},
        },
        headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
    )


def normalize_reg(reg):
    return reg.replace("-", "").replace(" ", "").upper()


def lookup_tar1090(registration):
    target = normalize_reg(registration)

    print("Downloading tar1090 aircraft database...")
    raw = request(TAR1090_URL)

    print("Searching database...")
    text = gzip.decompress(raw).decode("utf-8", errors="replace")

    for line in text.splitlines():
        parts = line.split(";", 5)

        if len(parts) < 5:
            continue

        if normalize_reg(parts[1]) != target:
            continue

        icao24 = parts[0].strip().lower()
        canonical_reg = parts[1].strip() or registration.upper()
        typecode = parts[2].strip()
        desc = parts[4].strip()

        if desc and typecode:
            type_name = f"{desc} ({typecode})"
        else:
            type_name = desc or typecode or "不明"

        return icao24, type_name, canonical_reg

    return None


def github_json(path, method="GET", data=None):
    raw = request(
        API + path,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    return json.loads(raw) if raw else None


def add_to_watchlist(icao24, registration, type_name):
    path = f"/repos/{REPO}/contents/watchlist.json"
    info = github_json(path)

    import base64

    content = base64.b64decode(info["content"]).decode()
    watchlist = json.loads(content)

    if icao24 in watchlist:
        return False

    watchlist[icao24] = {
        "label": registration,
        "type": type_name,
    }

    encoded = base64.b64encode(
        (json.dumps(watchlist, ensure_ascii=False, indent=4) + "\n").encode()
    ).decode()

    github_json(
        path,
        method="PUT",
        data={
            "message": f"watchlist: add {registration}",
            "content": encoded,
            "sha": info["sha"],
            "branch": "main",
        },
    )

    return True



# IATA航空会社コード → ICAOコールサイン
IATA_TO_ICAO = {
    "JL": "JAL",
    "NH": "ANA",
    "MM": "APJ",
    "GK": "JJP",
    "BC": "SKY",
    "7G": "SFJ",
    "NU": "JTA",
    "HD": "ADO",
    "IJ": "SJO",
    "6J": "SNJ",
    "FW": "IBX",
    "OC": "ORC",
    "3X": "JAC",
}
AIRPORT_SHORT_NAMES = {
    "NRT": "Narita", "HND": "Haneda", "NGO": "Chubu", "KIX": "Kansai",
    "ITM": "Itami", "CTS": "New Chitose", "FUK": "Fukuoka", "OKA": "Naha",
}


def callsign_candidates(text):
    import re

    value = str(text).strip().upper().replace(" ", "").replace("-", "")
    candidates = []

    m = re.fullmatch(r"([A-Z0-9]{2})(\d{1,4}[A-Z]?)", value)
    if m and m.group(1) in IATA_TO_ICAO:
        candidates.append(IATA_TO_ICAO[m.group(1)] + m.group(2))

    candidates.append(value)
    return list(dict.fromkeys(candidates))


def lookup_live_callsign(query):
    """adsb.lol から現在飛行中の機体を検索する。"""
    for callsign in callsign_candidates(query):
        url = (
            "https://api.adsb.lol/v2/callsign/"
            + urllib.parse.quote(callsign)
        )

        print(f"ADS-B lookup: {callsign}")

        try:
            raw = request(url)
            data = json.loads(raw)
        except urllib.error.HTTPError as e:
            print(f"ADS-B HTTP error: {e.code} ({callsign})")
            continue
        except Exception as e:
            print(f"ADS-B lookup failed: {type(e).__name__}: {e}")
            continue

        aircraft = data.get("ac") or []
        if aircraft:
            return aircraft[0]

    return None



def lookup_flight_route(callsign):
    """ADSBDBから便名・航空会社・出発地・到着地を取得する。"""
    callsign = str(callsign or "").strip().upper()

    if not callsign:
        return None

    url = (
        "https://api.adsbdb.com/v0/callsign/"
        + urllib.parse.quote(callsign)
    )

    print(f"Route lookup: {callsign}")

    try:
        raw = request(url)
        data = json.loads(raw)
        return data.get("response", {}).get("flightroute")
    except urllib.error.HTTPError as e:
        print(f"Route HTTP error: {e.code} ({callsign})")
    except Exception as e:
        print(f"Route lookup failed: {type(e).__name__}: {e}")

    return None

def format_live_aircraft(ac):
    callsign = str(ac.get("flight") or "").strip() or "不明"
    registration = ac.get("r") or "不明"
    icao24 = str(ac.get("hex") or "").lower() or "不明"
    aircraft_type = ac.get("desc") or ac.get("t") or "不明"

    route = lookup_flight_route(callsign) if callsign != "不明" else None

    lines = []

    if route:
        callsign_icao = route.get("callsign_icao") or callsign
        callsign_iata = route.get("callsign_iata")

        if callsign_iata:
            lines.append(f"✈️ **{callsign_iata} / {callsign_icao}**")
        else:
            lines.append(f"✈️ **{callsign_icao}**")

        airline = route.get("airline") or {}
        airline_name = airline.get("name")
        if airline_name:
            lines.append(f"航空会社: {airline_name}")

        origin = route.get("origin") or {}
        destination = route.get("destination") or {}

        if origin and destination:
            origin_iata = origin.get("iata_code") or "---"
            destination_iata = destination.get("iata_code") or "---"
            origin_name = (
                AIRPORT_SHORT_NAMES.get(origin_iata.upper())
                or origin.get("name")
                or origin.get("municipality")
                or "不明"
            )
            destination_name = (
                AIRPORT_SHORT_NAMES.get(destination_iata.upper())
                or destination.get("name")
                or destination.get("municipality")
                or "不明"
            )

            lines.append(
                f"区間: {origin_name} ({origin_iata})"
                f" → {destination_name} ({destination_iata})"
            )

        lines.append("")
    else:
        lines.append(f"✈️ **{callsign}** の現在情報")

    lines.extend([
        f"登録記号: `{registration}`",
        f"icao24: `{icao24}`",
        f"機種: {aircraft_type}",
    ])

    livery_name = lookup_livery(registration)
    if livery_name:
        lines.append(f"🎨 塗装名: {livery_name}")

    alt = ac.get("alt_baro")
    if alt == "ground":
        lines.append("状態: 地上")
    elif isinstance(alt, (int, float)):
        lines.append(f"高度: {round(alt):,} ft")

    speed = ac.get("gs")
    if isinstance(speed, (int, float)):
        lines.append(
            f"速度: {round(speed * 1.852):,} km/h "
            f"({round(speed):,} kt)"
        )

    track = ac.get("track")
    if isinstance(track, (int, float)):
        lines.append(f"進行方向: {round(track)}°")

    lat = ac.get("lat")
    lon = ac.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        lines.append(f"位置: {lat:.4f}, {lon:.4f}")
        lines.append(
            "地図: "
            f"https://globe.adsbexchange.com/?icao={icao24}"
        )

    return "\n".join(lines)


def main():
    payload = json.loads(os.environ["LOOKUP_PAYLOAD"])

    op = payload.get("op")
    args = payload.get("args") or []
    channel_id = str(payload.get("channel_id") or "")

    if not channel_id:
        raise RuntimeError("channel_id is missing")

    if not args:
        raise RuntimeError("args is missing")

    if op == "add":
        registration = str(args[0]).strip().upper()
        supplied_type = str(args[1]).strip() if len(args) > 1 else None

        found = lookup_tar1090(registration)

        if not found:
            discord_send(
                channel_id,
                f"⚠️ `{registration}` のicao24が自動取得できませんでした。"
                f"`/add tail:{registration} icao24:<icao24>` の形で手動指定してください。",
            )
            return

        icao24, db_type, canonical_reg = found
        type_name = supplied_type or db_type or "不明"

        added = add_to_watchlist(
            icao24,
            canonical_reg,
            type_name,
        )

        if added:
            discord_send(
                channel_id,
                f"✅ `{canonical_reg}` ({icao24} / {type_name}) をwatchlistに追加しました。",
            )
        else:
            discord_send(
                channel_id,
                f"ℹ️ `{canonical_reg}` ({icao24}) は既に登録済みです。",
            )

        return

    if op == "flight":
        query = str(args[0]).strip().upper()

        # まずリアルタイムADS-Bを検索
        live = lookup_live_callsign(query)

        if live:
            discord_send(
                channel_id,
                format_live_aircraft(live),
            )
            return

        # 登録記号の場合はtar1090の機体DBも検索
        found = lookup_tar1090(query)

        if found:
            icao24, type_name, canonical_reg = found
            discord_send(
                channel_id,
                f"🔎 `{canonical_reg}` の機体情報を確認しました。\n"
                f"icao24: `{icao24}`\n"
                f"機種: {type_name}\n"
                f"現在位置はADS-Bで確認できませんでした。",
            )
            return

        discord_send(
            channel_id,
            f"❓ `{query}` の現在のADS-B情報を取得できませんでした。",
        )
        return

    raise RuntimeError(f"unknown operation: {op}")


if __name__ == "__main__":
    main()
