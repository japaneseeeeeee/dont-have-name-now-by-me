import json
import fcntl
import logging
import math
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


# self-hosted GitHub Actionsのチェックアウトから実行するときも、Mac本体に保存した
# Webhook設定を利用する。AIRCRAFT_ENV_FILEを指定すれば別の場所にも移行できる。
PRIMARY_ENV_PATH = os.path.expanduser(
    os.environ.get("AIRCRAFT_ENV_FILE", "~/aircraft-alert/.env")
)
load_env_file(PRIMARY_ENV_PATH)
if os.path.abspath(PRIMARY_ENV_PATH) != os.path.join(BASE_DIR, ".env"):
    load_env_file(os.path.join(BASE_DIR, ".env"))

# ============ CONFIG ============

# 秘密情報は .env(または環境変数)から読む。コードには書かない。
CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET")
JAPAN_WEBHOOK_URL = os.environ.get("AIRCRAFT_WEBHOOK_URL")

# 日本全体を1回だけ取得し、最も近い地方の監視範囲へ振り分ける。
JAPAN_BBOX = {
    "lamin": 23.5,
    "lomin": 122.0,
    "lamax": 46.0,
    "lomax": 146.0,
}

# 従来の「日本周辺」早期警戒範囲。全国取得した結果から切り出すためAPI追加取得はしない。
EARLY_WARNING_BBOX = (34.0, 138.2, 37.5, 143.1)

REGIONS = {
    "hokkaido": {
        "name": "北海道", "env": "AIRCRAFT_WEBHOOK_HOKKAIDO",
        "bbox": (41.2, 139.0, 45.8, 146.0),
    },
    "tohoku": {
        "name": "東北", "env": "AIRCRAFT_WEBHOOK_TOHOKU",
        "bbox": (36.7, 138.5, 41.6, 142.5),
    },
    "kanto": {
        "name": "関東", "env": "AIRCRAFT_WEBHOOK_KANTO",
        "bbox": (34.7, 138.0, 37.3, 141.8),
    },
    "chubu": {
        "name": "中部", "env": "AIRCRAFT_WEBHOOK_CHUBU",
        "bbox": (34.0, 135.3, 38.7, 140.0),
    },
    "kinki": {
        "name": "近畿", "env": "AIRCRAFT_WEBHOOK_KINKI",
        "bbox": (33.2, 134.0, 36.0, 137.0),
    },
    "chugoku_shikoku": {
        "name": "中国・四国", "env": "AIRCRAFT_WEBHOOK_CHUGOKU_SHIKOKU",
        "bbox": (32.5, 130.5, 35.8, 135.2),
    },
    "kyushu": {
        "name": "九州", "env": "AIRCRAFT_WEBHOOK_KYUSHU",
        "bbox": (29.0, 128.0, 34.5, 132.2),
    },
    "okinawa": {
        "name": "沖縄", "env": "AIRCRAFT_WEBHOOK_OKINAWA",
        "bbox": (23.5, 122.0, 29.0, 131.5),
    },
}

for region in REGIONS.values():
    region["webhook"] = os.environ.get(region["env"]) if region["env"] else None

# 既存関数を直接利用するコード向けの既定表示。
AIRPORT_NAME = "関東"

WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
# すべての地方で同じGitHub上のwatchlistを正とする。ネットワーク障害時だけ
# ローカルの最後のコピーへ安全にフォールバックする。
SHARED_WATCHLIST_URL = os.environ.get(
    "SHARED_WATCHLIST_URL",
    "https://raw.githubusercontent.com/japaneseeeeeee/dont-have-name-now-by-me/main/watchlist.json",
)

# 通知済みの機体を記録しておくファイル(同じ機体を何度も通知しないため)
STATE_PATH = os.path.join(BASE_DIR, "notified.json")
PERSONAL_SPECIALS_PATH = os.path.join(BASE_DIR, "personal_specials.json")
PERSONAL_SPECIAL_STATE_PATH = os.path.join(BASE_DIR, "personal_special_notified.json")
PERSONAL_SPECIAL_EVENTS_PATH = os.path.join(BASE_DIR, "personal_special_events.json")

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
COLOR_WATCH = 0xF39C12     # オレンジ
COLOR_SPECIAL = 0xE74C3C   # 赤
PRIORITIES = {"NORMAL", "WATCH", "SPECIAL"}
AIRPORT_SHORT_NAMES = {
    "NRT": "Narita", "HND": "Haneda", "NGO": "Chubu", "KIX": "Kansai",
    "ITM": "Itami", "CTS": "New Chitose", "FUK": "Fukuoka", "OKA": "Naha",
}

# ============ ここまで CONFIG ============


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("aircraft-alert")


def normalize_priority(value):
    """不正値や旧形式を安全に NORMAL として扱う。"""
    priority = str(value or "NORMAL").upper()
    return priority if priority in PRIORITIES else "NORMAL"


def effective_priority(entry, now=None):
    """期限付きSPECIALを考慮した現在の優先度を返す。"""
    priority = normalize_priority(entry.get("priority"))
    if priority != "SPECIAL" or not entry.get("special_until"):
        return priority
    try:
        expires_at = float(entry["special_until"])
    except (TypeError, ValueError):
        return priority
    if (time.time() if now is None else now) < expires_at:
        return "SPECIAL"
    return normalize_priority(entry.get("priority_after_special"))


def classify_region(lat, lon):
    """緯度経度を地方キーへ振り分ける。範囲が重なる場合は中心に最も近い地方を選ぶ。"""
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    candidates = []
    for key, region in REGIONS.items():
        lamin, lomin, lamax, lomax = region["bbox"]
        if not (lamin <= lat <= lamax and lomin <= lon <= lomax):
            continue
        lat_span = lamax - lamin
        lon_span = lomax - lomin
        lat_center = (lamin + lamax) / 2
        lon_center = (lomin + lomax) / 2
        distance = ((lat - lat_center) / lat_span) ** 2 + ((lon - lon_center) / lon_span) ** 2
        candidates.append((distance, key))
    return min(candidates)[1] if candidates else None


def is_inside_bbox(aircraft, bbox):
    """OpenSky state vectorに位置があり、指定範囲内ならTrue。"""
    lat, lon = aircraft[6], aircraft[5]
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    lamin, lomin, lamax, lomax = bbox
    return lamin <= lat <= lamax and lomin <= lon <= lomax


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
    """GitHubの共通watchlistを読み込む(障害時のみローカルへフォールバック)。"""
    try:
        response = requests.get(SHARED_WATCHLIST_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict):
            raise ValueError("watchlist must be an object")
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        logger.warning("共通watchlistを取得できないためローカルコピーを使用: %s", exc)
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)

    watchlist = {}
    for icao24, value in raw.items():
        if isinstance(value, dict):
            label = value.get("label", icao24)
            aircraft_type = value.get("type") or UNKNOWN_TYPE
            priority = effective_priority(value)
        else:
            label = value
            aircraft_type = UNKNOWN_TYPE
            priority = "NORMAL"
        watchlist[icao24] = {"label": label, "type": aircraft_type, "priority": priority}

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


def find_new_region_detections(states, watchlist, notified, now):
    """登録機を地方へ振り分け、地方ごとに重複通知を管理する。"""
    notified = dict(notified)
    present = set()
    to_notify = []

    for aircraft in states:
        icao24 = (aircraft[0] or "").strip().lower()
        if icao24 not in watchlist:
            continue
        region_key = classify_region(aircraft[6], aircraft[5])
        if region_key is None:
            continue
        state_key = f"{region_key}:{icao24}"
        present.add(state_key)

        # 旧形式の通知時刻も初回だけ引き継ぎ、切替直後の重複通知を防ぐ。
        last = notified.get(state_key, notified.get(icao24))
        on_ground = bool(aircraft[8])
        if last is None:
            to_notify.append((region_key, icao24, aircraft, False))
            notified[state_key] = now
        elif now - last >= RENOTIFY_SECONDS and (RENOTIFY_ON_GROUND or not on_ground):
            to_notify.append((region_key, icao24, aircraft, True))
            notified[state_key] = now
        elif state_key not in notified:
            notified[state_key] = last

    region_prefixes = tuple(f"{key}:" for key in REGIONS)
    notified = {
        key: timestamp for key, timestamp in notified.items()
        if not key.startswith(region_prefixes)
        or key in present
        or now - timestamp < RENOTIFY_SECONDS
    }
    return to_notify, notified, present


def find_new_area_detections(states, watchlist, notified, now, scope):
    """日本周辺など、地方とは別の監視範囲の重複通知を管理する。"""
    notified = dict(notified)
    present = set()
    to_notify = []
    prefix = f"{scope}:"
    for aircraft in states:
        icao24 = (aircraft[0] or "").strip().lower()
        if icao24 not in watchlist:
            continue
        state_key = f"{prefix}{icao24}"
        present.add(state_key)
        last = notified.get(state_key, notified.get(icao24))
        on_ground = bool(aircraft[8])
        if last is None:
            to_notify.append((icao24, aircraft, False))
            notified[state_key] = now
        elif now - last >= RENOTIFY_SECONDS and (RENOTIFY_ON_GROUND or not on_ground):
            to_notify.append((icao24, aircraft, True))
            notified[state_key] = now
        elif state_key not in notified:
            notified[state_key] = last
    notified = {
        key: timestamp for key, timestamp in notified.items()
        if not key.startswith(prefix) or key in present or now - timestamp < RENOTIFY_SECONDS
    }
    return to_notify, notified, present


def should_send_japan_alert(aircraft, entry):
    """全国通知を明示的に有効化した登録機だけを日本周辺へ通知する。"""
    return isinstance(entry, dict) and entry.get("nationwide_alert") is True


def load_shared_json(path, default):
    """Botと共有するJSONをロック付きで読み込む。"""
    lock_path = path + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        try:
            if not os.path.exists(path):
                return default.copy() if isinstance(default, dict) else list(default)
            with open(path, "r", encoding="utf-8") as data_file:
                return json.load(data_file)
        except (OSError, ValueError):
            return default.copy() if isinstance(default, dict) else list(default)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def save_shared_json(path, data):
    """Botと共有するJSONをロック付きで安全に置き換える。"""
    lock_path = path + ".lock"
    tmp_path = path + ".tmp"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            with open(tmp_path, "w", encoding="utf-8") as data_file:
                json.dump(data, data_file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def append_personal_special_events(events):
    """検出イベントをBotのDM送信キューへ追加する。"""
    if not events:
        return
    path = PERSONAL_SPECIAL_EVENTS_PATH
    lock_path = path + ".lock"
    tmp_path = path + ".tmp"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            queued = []
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as data_file:
                        queued = json.load(data_file)
                except (OSError, ValueError):
                    queued = []
            queued = queued if isinstance(queued, list) else []
            with open(tmp_path, "w", encoding="utf-8") as data_file:
                json.dump(queued + events, data_file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def find_personal_special_events(states, settings, notified, now):
    """個人SPECIAL登録機を日本国内で検出し、ユーザー別DMイベントを作る。"""
    notified = dict(notified)
    aircraft_by_icao = {
        (aircraft[0] or "").strip().lower(): aircraft
        for aircraft in states
        if aircraft and aircraft[0]
    }
    active_keys = set()
    events = []
    for user_id, config in settings.items():
        if not isinstance(config, dict) or config.get("enabled", True) is False:
            continue
        registered = config.get("aircraft") or {}
        for icao24, entry in registered.items():
            icao24 = icao24.lower()
            aircraft = aircraft_by_icao.get(icao24)
            if aircraft is None:
                continue
            region_key = classify_region(aircraft[6], aircraft[5])
            if region_key is None:
                continue
            state_key = f"{user_id}:{icao24}"
            active_keys.add(state_key)
            last = notified.get(state_key)
            on_ground = bool(aircraft[8])
            repeat = last is not None
            if last is not None and (
                now - last < RENOTIFY_SECONDS or (on_ground and not RENOTIFY_ON_GROUND)
            ):
                continue
            notified[state_key] = now
            entry = entry if isinstance(entry, dict) else {}
            events.append({
                "user_id": str(user_id),
                "icao24": icao24,
                "label": entry.get("label") or icao24,
                "type": entry.get("type") or "不明",
                "region": REGIONS[region_key]["name"],
                "repeat": repeat,
                "callsign": (aircraft[1] or "").strip(),
                "longitude": aircraft[5],
                "latitude": aircraft[6],
                "altitude": aircraft[7],
                "on_ground": on_ground,
                "velocity": aircraft[9],
                "track": aircraft[10],
                "vertical_rate": aircraft[11],
                "detected_at": now,
            })

    notified = {
        key: timestamp for key, timestamp in notified.items()
        if key in active_keys or now - timestamp < RENOTIFY_SECONDS
    }
    return events, notified


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
    iata = (airport.get("iata_code") or "").upper()
    code = iata or airport.get("icao_code") or "?"
    name = AIRPORT_SHORT_NAMES.get(iata) or airport.get("name") or airport.get("municipality") or ""
    return f"{name} ({code})" if name else code


def haversine_km(lat1, lon1, lat2, lon2):
    """2地点間の大圏距離(km)。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _latlon_vector(lat, lon):
    phi, lam = math.radians(lat), math.radians(lon)
    return (math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))


def _vector_latlon(vector):
    x, y, z = vector
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def route_matches_position(route, lat, lon, max_distance_km=600):
    """現在位置が空港間の大圏経路から大きく外れていないか判定する。"""
    if not route or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    try:
        origin = route["origin"]
        destination = route["destination"]
        lat1, lon1 = float(origin["latitude"]), float(origin["longitude"])
        lat2, lon2 = float(destination["latitude"]), float(destination["longitude"])
    except (KeyError, TypeError, ValueError):
        return False

    start = _latlon_vector(lat1, lon1)
    end = _latlon_vector(lat2, lon2)
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(start, end))))
    omega = math.acos(dot)
    sin_omega = math.sin(omega)
    samples = 72
    nearest = float("inf")
    for index in range(samples + 1):
        fraction = index / samples
        if abs(sin_omega) < 1e-9:
            vector = tuple((1 - fraction) * a + fraction * b for a, b in zip(start, end))
        else:
            left = math.sin((1 - fraction) * omega) / sin_omega
            right = math.sin(fraction * omega) / sin_omega
            vector = tuple(left * a + right * b for a, b in zip(start, end))
        sample_lat, sample_lon = _vector_latlon(vector)
        nearest = min(nearest, haversine_km(lat, lon, sample_lat, sample_lon))
    return nearest <= max_distance_km


def format_detection_reason(priority, region_name, repeat=False):
    """通知レベルと検出状態を、自然な日本語の説明にする。"""
    aircraft_label = {
        "SPECIAL": "特別注目機（SPECIAL）",
        "WATCH": "注目機（WATCH）",
        "NORMAL": "登録機",
    }.get(priority, "登録機")
    status = "引き続き検出しています" if repeat else "新たに検出しました"
    return f"{aircraft_label}を{region_name}の監視範囲内で{status}。"


def build_embed(
    icao24, entry, aircraft, photo=None, route=None, repeat=False,
    region_name=AIRPORT_NAME,
):
    """OpenSkyの state vector から Discord Embed(dict)を組み立てる。"""
    label = entry["label"]
    aircraft_type = entry["type"]
    priority = effective_priority(entry)

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
        {"name": "通知レベル", "value": priority, "inline": True},
        {
            "name": "検出理由",
            "value": format_detection_reason(priority, region_name, repeat),
            "inline": False,
        },
    ]

    if route and route_matches_position(route, lat, lon):
        flight = f"{route['flight_iata']} · " if route.get("flight_iata") else ""
        fields.append({
            "name": "区間(推定)",
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

    if priority == "SPECIAL":
        title_prefix, color = "🚨 SPECIAL AIRCRAFT", COLOR_SPECIAL
    elif priority == "WATCH":
        title_prefix, color = "🟠 WATCH AIRCRAFT", COLOR_WATCH
    else:
        title_prefix = "✈️"
        color = COLOR_GROUND if on_ground else COLOR_AIRBORNE

    embed = {
        "title": f"{title_prefix} {label} を検知" + ("(範囲内に継続中)" if repeat else ""),
        "url": f"https://globe.adsbexchange.com/?icao={icao24}",
        "description": (
            f"**{'地上(到着/駐機中)' if on_ground else '飛行中'}** · {region_name}"
            + (f"\n位置: {lat:.3f}, {lon:.3f}" if lat is not None and lon is not None else "")
        ),
        "color": color,
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


def notify_discord(icao24, entry, aircraft, webhook_url, region_name, repeat=False):
    label = entry["label"]
    aircraft_type = entry["type"]
    priority = effective_priority(entry)

    photo = fetch_photo(icao24)
    route = fetch_route((aircraft[1] or "").strip())
    route_verified = route if route_matches_position(route, aircraft[6], aircraft[5]) else None
    embed = build_embed(icao24, entry, aircraft, photo, route_verified, repeat, region_name)

    payload = {
        # スマホのプッシュ通知プレビューはcontentが表示されるため入れておく
        "content": (
            ("🚨 **SPECIAL**" if priority == "SPECIAL" else "🟠 **WATCH**" if priority == "WATCH" else "✈️")
            + f" **{label}** ({aircraft_type}) を検知"
            + ("(範囲内に継続中)" if repeat else "")
        ),
        "embeds": [embed],
    }
    try:
        response = request_with_retry("post", webhook_url, json=payload)
        logger.info(
            "Discord通知: %s (%s / %s) %s 写真=%s 区間=%s",
            response.status_code, f"{region_name}/{label}", aircraft_type,
            "再通知" if repeat else "初回",
            "あり" if photo else "なし", "あり" if route_verified else "不一致/なし",
        )
    except requests.exceptions.RequestException as exc:
        logger.error("Discord通知に失敗しました(%s): %s", label, exc)


def main():
    logger.info("チェック開始")

    missing = [name for name, value in (
        ("OPENSKY_CLIENT_ID", CLIENT_ID),
        ("OPENSKY_CLIENT_SECRET", CLIENT_SECRET),
        ("AIRCRAFT_WEBHOOK_URL", JAPAN_WEBHOOK_URL),
        *((region["env"], region["webhook"]) for region in REGIONS.values() if region["env"]),
    ) if not value]
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
        response = request_with_retry("get", url, params=JAPAN_BBOX, headers=headers)
    except requests.exceptions.RequestException as exc:
        logger.error("API取得に失敗しました(リトライ上限到達): %s", exc)
        return

    if response.status_code != 200:
        logger.error("APIエラー: %d / 詳細: %s", response.status_code, response.text)
        return

    data = response.json()
    states = data.get("states") or []

    # メンバーごとの個人SPECIALを通常通知とは独立して判定し、
    # Botが本人へDMするためのイベントキューへ渡す。
    personal_settings = load_shared_json(PERSONAL_SPECIALS_PATH, {})
    personal_notified = load_shared_json(PERSONAL_SPECIAL_STATE_PATH, {})
    personal_events, personal_notified = find_personal_special_events(
        states, personal_settings, personal_notified, time.time()
    )
    save_shared_json(PERSONAL_SPECIAL_STATE_PATH, personal_notified)
    append_personal_special_events(personal_events)

    to_notify, notified, currently_present = find_new_region_detections(
        states, watchlist, notified, time.time()
    )
    region_notifications = set()
    for region_key, icao24, aircraft, repeat in to_notify:
        region = REGIONS[region_key]
        if not region["webhook"]:
            continue
        notify_discord(
            icao24, watchlist[icao24], aircraft,
            region["webhook"], region["name"], repeat,
        )
        region_notifications.add((icao24, region["webhook"]))

    # /nationwide で明示的に有効化した機体だけを日本全域で監視し、
    # 日本周辺チャンネルへ知らせる。SPECIALとは独立した設定。
    early_states = []
    for aircraft in states:
        icao24 = (aircraft[0] or "").strip().lower()
        entry = watchlist.get(icao24)
        if entry and should_send_japan_alert(aircraft, entry):
            early_states.append(aircraft)
    early_notify, notified, early_present = find_new_area_detections(
        early_states, watchlist, notified, time.time(), "nationwide"
    )
    for icao24, aircraft, repeat in early_notify:
        # Webhookの設定ミスで日本周辺と地方が同じチャンネルを指していても、
        # 1回の監視中に同じ機体を同じ送信先へ二重投稿しない。
        if (icao24, JAPAN_WEBHOOK_URL) in region_notifications:
            logger.warning(
                "日本周辺通知を省略: %s は同じWebhookへ地方通知済みです", icao24
            )
            continue
        notify_discord(
            icao24, watchlist[icao24], aircraft,
            JAPAN_WEBHOOK_URL,
            "日本国内(全国通知)",
            repeat,
        )

    save_notified(notified)

    logger.info(
        "チェック完了。地方別=%s / 日本周辺=%s / 個人SPECIAL=%d件",
        currently_present or "なし", early_present or "なし", len(personal_events),
    )


if __name__ == "__main__":
    main()
