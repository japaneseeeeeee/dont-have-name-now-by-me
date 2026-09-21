import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ============ .env 読み込み(依存ライブラリなし) ============

def load_env_file(path):
    """KEY=VALUE 形式の .env を読み込み、未設定の環境変数だけ埋める。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(os.path.join(BASE_DIR, ".env"))

# ============ CONFIG ============

# 秘密情報は .env(または環境変数)から読む。コードには書かない。
WEBHOOK_URL = os.environ.get("AIRCRAFT_WEBHOOK_URL")
CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET")

# 監視する範囲
BBOX = {
    "lamin": 20,
    "lomin": 113,
    "lamax": 46,
    "lomax": 154,
}

AIRPORT_NAME = "日本周辺"

WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")

# 通知済みの機体を記録しておくファイル(同じ機体を何度も通知しないため)
STATE_PATH = os.path.join(BASE_DIR, "notified.json")

# リトライ設定
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_TIMEOUT = 15  # 秒

# 写真取得は通知を遅らせないよう短めのタイムアウトでリトライなし
PHOTO_TIMEOUT = 5
ROUTE_TIMEOUT = 5

UNKNOWN_TYPE = "不明"

# 前回の通知からこの時間(秒)たって、まだ範囲内にいたら、もう一度通知する。
# 範囲外に出ていた機体も、この時間を過ぎていれば、戻ってきたときに通知する。
RENOTIFY_SECONDS = 20 * 60

# 地上(駐機中など)の機体を再通知するか。Falseなら最初の1回だけ通知し、
# 何時間も駐機している機体から20分おきに通知が来るのを防ぐ。
RENOTIFY_ON_GROUND = False

COLOR_AIRBORNE = 0x3498DB  # 青
COLOR_GROUND = 0x2ECC71    # 緑

# ============ ここまで CONFIG ============


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("aircraft-alert")


def request_with_retry(method, url, **kwargs):
    """requestsのリクエストをリトライ付きで実行する。"""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * attempt
                logger.warning(
                    "リクエスト失敗(%s, %d/%d回目): %s -> %d秒後にリトライ",
                    url, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "リクエスト失敗(%s, %d/%d回目、リトライ上限到達): %s",
                    url, attempt, MAX_RETRIES, exc,
                )

    raise last_exc


def load_watchlist():
    """watchlist.json を読み込む(文字列形式・dict形式の混在OK)。"""
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    watchlist = {}
    for icao24, value in raw.items():
        if isinstance(value, dict):
            label = value.get("label", icao24)
            aircraft_type = value.get("type") or UNKNOWN_TYPE
        else:
            label = value
            aircraft_type = UNKNOWN_TYPE
        watchlist[icao24] = {"label": label, "type": aircraft_type}

    return watchlist


def load_notified():
    """通知済みの機体を {icao24: 最後に通知した時刻(epoch秒)} で返す。旧形式(リスト)も読める。"""
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    now = time.time()
    if isinstance(raw, list):
        return {str(k): now for k in raw}
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items()}
    return {}


def save_notified(notified):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(notified, f, ensure_ascii=False)


def find_new_detections(states, watchlist, notified, now):
    """範囲内の機体から、今回通知すべきものを選ぶ。

    - 初めて見た機体: 通知する
    - 前回の通知から RENOTIFY_SECONDS 以上たっていて、まだ範囲内にいる機体: 再通知する
      (地上の機体は RENOTIFY_ON_GROUND が True のときだけ)
    - 範囲外にいて、前回の通知から RENOTIFY_SECONDS 以上たった機体は記録から外す
      (次に現れたときは、初めてとして通知する)

    戻り値: ([(icao24, state, 再通知か)], 更新後のnotified, 今回範囲内にいたicao24の集合)
    """
    notified = dict(notified)
    present = set()
    to_notify = []

    for aircraft in states:
        icao24 = (aircraft[0] or "").strip().lower()
        if icao24 not in watchlist:
            continue
        present.add(icao24)

        last = notified.get(icao24)
        on_ground = bool(aircraft[8])
        if last is None:
            to_notify.append((icao24, aircraft, False))
            notified[icao24] = now
        elif now - last >= RENOTIFY_SECONDS and (RENOTIFY_ON_GROUND or not on_ground):
            to_notify.append((icao24, aircraft, True))
            notified[icao24] = now

    notified = {
        k: t for k, t in notified.items()
        if k in present or now - t < RENOTIFY_SECONDS
    }
    return to_notify, notified, present


# ============ 通知(Embed) ============

_COMPASS = ["北", "北東", "東", "南東", "南", "南西", "西", "北西"]


def compass(deg):
    return _COMPASS[int((deg + 22.5) // 45) % 8]


def fetch_photo(icao24):
    """Planespotters の公開APIから機体写真を1枚取得する。なければNone。"""
    try:
        r = requests.get(
            f"https://api.planespotters.net/pub/photos/hex/{icao24}",
            timeout=PHOTO_TIMEOUT,
            headers={"User-Agent": "aircraft-alert/1.0"},
        )
        if r.status_code != 200:
            return None
        photos = r.json().get("photos") or []
        if not photos:
            return None
        p = photos[0]
        thumb = (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("src")
        if not thumb:
            return None
        return {
            "src": thumb,
            "link": p.get("link"),
            "photographer": p.get("photographer"),
        }
    except (requests.RequestException, ValueError):
        return None


def fetch_route(callsign):
    """adsbdb のコールサイン→予定ルートDBから、出発地・到着地を取得する。なければNone。

    ADS-B の電波には出発地/到着地は含まれないので、コールサイン(JAL123など)から
    「その便の予定ルート」を引いている。不定期便・軍用機・貨物・プライベート機は
    載っていないことが多く、同じコールサインで複数区間を飛ぶ便は外れることもある。
    """
    callsign = (callsign or "").strip()
    if not callsign:
        return None
    try:
        r = requests.get(
            f"https://api.adsbdb.com/v0/callsign/{callsign}",
            timeout=ROUTE_TIMEOUT,
            headers={"User-Agent": "aircraft-alert/1.0"},
        )
        if r.status_code != 200:
            return None
        body = r.json().get("response")
        flightroute = body.get("flightroute") if isinstance(body, dict) else None
        if not flightroute or not flightroute.get("origin") or not flightroute.get("destination"):
            return None
        return {
            "origin": flightroute["origin"],
            "destination": flightroute["destination"],
            "flight_iata": flightroute.get("callsign_iata"),
            "airline": (flightroute.get("airline") or {}).get("name"),
        }
    except (requests.RequestException, ValueError, AttributeError):
        return None


def format_airport(airport):
    code = airport.get("iata_code") or airport.get("icao_code") or "?"
    place = airport.get("municipality") or airport.get("name") or ""
    return f"{place} ({code})" if place else code


def build_embed(icao24, entry, aircraft, photo=None, route=None, repeat=False):
    """OpenSkyの state vector から Discord Embed(dict)を組み立てる。"""
    label = entry["label"]
    aircraft_type = entry["type"]

    callsign = (aircraft[1] or "").strip()
    lon, lat = aircraft[5], aircraft[6]
    altitude = aircraft[7]
    on_ground = aircraft[8]
    velocity = aircraft[9]
    track = aircraft[10]
    vrate = aircraft[11]

    fields = [
        {"name": "機種", "value": aircraft_type, "inline": True},
        {"name": "コールサイン", "value": f"`{callsign or '不明'}`", "inline": True},
        {"name": "icao24", "value": f"`{icao24}`", "inline": True},
    ]

    if route:
        flight = f"{route['flight_iata']} · " if route.get("flight_iata") else ""
        fields.append({
            "name": "区間(予定)",
            "value": f"{flight}{format_airport(route['origin'])} → {format_airport(route['destination'])}",
            "inline": False,
        })

    if not on_ground:
        if altitude is not None:
            fields.append({
                "name": "高度",
                "value": f"{altitude:,.0f} m ({altitude * 3.28084:,.0f} ft)",
                "inline": True,
            })
        if velocity is not None:
            fields.append({
                "name": "速度",
                "value": f"{velocity * 3.6:,.0f} km/h ({velocity * 1.94384:,.0f} kt)",
                "inline": True,
            })
        if track is not None:
            fields.append({
                "name": "進行方向",
                "value": f"{compass(track)} ({track:.0f}°)",
                "inline": True,
            })
        if vrate is not None and abs(vrate) >= 1:
            arrow = "↑ 上昇" if vrate > 0 else "↓ 降下"
            fields.append({
                "name": "垂直速度",
                "value": f"{arrow} {abs(vrate):.1f} m/s",
                "inline": True,
            })

    embed = {
        "title": f"✈️ {label} を検知" + ("(範囲内に継続中)" if repeat else ""),
        "url": f"https://globe.adsbexchange.com/?icao={icao24}",
        "description": (
            f"**{'地上(到着/駐機中)' if on_ground else '飛行中'}** · {AIRPORT_NAME}"
            + (f"\n位置: {lat:.3f}, {lon:.3f}" if lat is not None and lon is not None else "")
        ),
        "color": COLOR_GROUND if on_ground else COLOR_AIRBORNE,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if photo:
        embed["image"] = {"url": photo["src"]}
        credit = photo.get("photographer") or "不明"
        embed["footer"] = {"text": f"Photo: {credit} / Planespotters.net"}
    else:
        embed["footer"] = {"text": "タイトルをタップで地図(ADS-B Exchange)"}

    return embed


def notify_discord(icao24, entry, aircraft, repeat=False):
    label = entry["label"]
    aircraft_type = entry["type"]

    photo = fetch_photo(icao24)
    route = fetch_route((aircraft[1] or "").strip())
    embed = build_embed(icao24, entry, aircraft, photo, route, repeat)

    payload = {
        # スマホのプッシュ通知プレビューはcontentが表示されるため入れておく
        "content": f"✈️ **{label}** ({aircraft_type}) を検知" + ("(範囲内に継続中)" if repeat else ""),
        "embeds": [embed],
    }
    try:
        response = request_with_retry("post", WEBHOOK_URL, json=payload)
        logger.info(
            "Discord通知: %s (%s / %s) %s 写真=%s 区間=%s",
            response.status_code, label, aircraft_type,
            "再通知" if repeat else "初回",
            "あり" if photo else "なし", "あり" if route else "なし",
        )
    except requests.exceptions.RequestException as exc:
        logger.error("Discord通知に失敗しました(%s): %s", label, exc)


def main():
    logger.info("チェック開始")

    missing = [
        name for name, value in (
            ("AIRCRAFT_WEBHOOK_URL", WEBHOOK_URL),
            ("OPENSKY_CLIENT_ID", CLIENT_ID),
            ("OPENSKY_CLIENT_SECRET", CLIENT_SECRET),
        ) if not value
    ]
    if missing:
        logger.error(".env に次の設定がありません: %s", ", ".join(missing))
        return

    watchlist = load_watchlist()
    notified = load_notified()

    # 1. OAuth2 トークンの取得
    token_url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    token_data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }

    try:
        token_res = request_with_retry("post", token_url, data=token_data)
    except requests.exceptions.RequestException as exc:
        logger.error("トークン取得に失敗しました(リトライ上限到達): %s", exc)
        return

    if token_res.status_code != 200:
        logger.error("トークン取得エラー: %d / 詳細: %s", token_res.status_code, token_res.text)
        return

    access_token = token_res.json().get("access_token")

    # 2. API へデータ要求
    url = "https://opensky-network.org/api/states/all"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        response = request_with_retry("get", url, params=BBOX, headers=headers)
    except requests.exceptions.RequestException as exc:
        logger.error("API取得に失敗しました(リトライ上限到達): %s", exc)
        return

    if response.status_code != 200:
        logger.error("APIエラー: %d / 詳細: %s", response.status_code, response.text)
        return

    data = response.json()
    states = data.get("states") or []
    to_notify, notified, currently_present = find_new_detections(
        states, watchlist, notified, time.time()
    )
    for icao24, aircraft, repeat in to_notify:
        notify_discord(icao24, watchlist[icao24], aircraft, repeat)

    save_notified(notified)

    logger.info("チェック完了。範囲内で検知した機体: %s", currently_present or "なし")


if __name__ == "__main__":
    main()
