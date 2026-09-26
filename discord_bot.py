"""
watchlist.json を Discord のコマンドで追加/削除/検索/一覧できるようにするBot。

コマンド:
  !add <登録記号> [icao24] [機種]
      例: !add JA08XJ
          !add JA08XJ 841FF4
          !add JA08XJ 841FF4 Boeing 777-300
          !add JA08XJ Boeing 777-300      (icao24を省略して機種だけ指定もOK)
      icao24を省略した場合は hexdb.io → tar1090-db(自動ダウンロード) → aircraftDatabase.csv の順で
      自動検索する。機種を省略した場合も同じ順で自動取得を試みる(失敗時は「不明」)。
      ハイフン無し(HSTYV)でも、大文字小文字が違っても登録できる。

  !remove <登録記号 または icao24>
  !find <キーワード>     登録記号/icao24に部分一致する機体を表示
  !list [ページ]         watchlist全体を20件ずつ表示
  !flight <便名など>     watchlistに無い機体も、今のADS-B位置を表示する
      例: !flight JL123     (IATA便名。JAL123に変換して検索)
          !flight JAL123    (コールサイン)
          !flight HS-TYV    (登録記号)   !flight 885336 (icao24)

切断対策:
  - 30秒おきに bot_heartbeat を更新する。watchdog.sh がこれを見て、止まっていたらBotを再起動する。
  - 再接続したとき、切断中に送られたコマンド(12時間以内)を拾って処理する(⏱️のリアクションが付く)。

既存の monitor.py / add_b1b_live.py と同じ watchlist.json を読み書きするため、
書き込みは flock + tmpファイル→rename で行い、同時書き込みによる破損を防いでいる。
"""

import asyncio
import csv
import fcntl
import gzip
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks

from route_corrections import correct_route

WATCHLIST_PATH = os.path.expanduser("~/aircraft-alert/watchlist.json")
# OpenSkyの aircraftDatabase.csv を置いておくと、hexdb.io で見つからない機体も登録できる
AIRCRAFT_DB_PATH = os.path.expanduser(
    os.environ.get("AIRCRAFT_DB_PATH", "~/aircraft-alert/aircraftDatabase.csv")
)
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# tar1090/adsb.lol が使っている機体DB(登録記号→icao24・機種。約60万機、毎日更新)
TAR1090_DB_URL = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"
TAR1090_DB_PATH = os.path.expanduser("~/aircraft-alert/tar1090-aircraft.csv.gz")
TAR1090_REFRESH_SECONDS = 24 * 3600  # 見つからなかったとき、これより古ければ再ダウンロード

BASE_DIR = os.path.expanduser("~/aircraft-alert")
STATE_PATH = os.path.join(BASE_DIR, "bot_state.json")          # チャンネルごとの最終処理メッセージID
HEARTBEAT_PATH = os.path.join(BASE_DIR, "bot_heartbeat")       # watchdog.sh が更新時刻を見る
PERSONAL_SPECIALS_PATH = os.path.join(BASE_DIR, "personal_specials.json")
PERSONAL_SPECIAL_EVENTS_PATH = os.path.join(BASE_DIR, "personal_special_events.json")
HEARTBEAT_INTERVAL = 30                                        # 秒
CATCHUP_MAX_AGE = timedelta(hours=12)                          # これより古い取りこぼしは処理しない
CATCHUP_LIMIT = 50                                             # 1回の再接続で拾う最大件数
FEEDBACK_SOURCE_CHANNEL_ID = int(os.environ.get("FEEDBACK_SOURCE_CHANNEL_ID", "1552183795187322962"))
FEEDBACK_DESTINATION_CHANNEL_ID = int(
    os.environ.get("FEEDBACK_DESTINATION_CHANNEL_ID", "1552647426253389926")
)
FEEDBACK_OWNER_ID = int(os.environ.get("FEEDBACK_OWNER_ID", "1083347827041771561"))
FEEDBACK_MARKER = "📮"
PHOTO_CHANNEL_ID = int(os.environ.get("PHOTO_CHANNEL_ID", "1552676837581389956"))
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")
PERSONAL_SPECIAL_LIMIT = 20

# 現在位置の検索に使うADS-B API(どちらも ADSBExchange v2 互換・登録不要)。上から順に試す。
ADSB_API_BASES = ["https://api.adsb.lol", "https://api.adsb.one"]
ADSB_TIMEOUT = 8
HTTP_HEADERS = {"User-Agent": "aircraft-alert-bot/1.0"}

# IATA航空会社コード → ICAOコード(コールサインの先頭3文字)。
# 「JL123」を「JAL123」に変換するために使う。足りない航空会社は追記してよい。
IATA_TO_ICAO = {
    # 日本
    "JL": "JAL", "NH": "ANA", "MM": "APJ", "GK": "JJP", "BC": "SKY", "7G": "SFJ",
    "NU": "JTA", "HD": "ADO", "IJ": "SJO", "6J": "SNJ", "FW": "IBX", "OC": "ORC",
    "3X": "JAC",
    # アジア・オセアニア
    "KE": "KAL", "OZ": "AAR", "7C": "JJA", "LJ": "JNA", "TW": "TWB", "BX": "ABL",
    "CX": "CPA", "UO": "HKE", "HX": "CRK", "CI": "CAL", "BR": "EVA", "IT": "TTW",
    "CA": "CCA", "MU": "CES", "CZ": "CSN", "HU": "CHH", "3U": "CSC",
    "SQ": "SIA", "TR": "TGW", "TG": "THA", "FD": "AIQ", "VN": "HVN", "VJ": "VJC",
    "PR": "PAL", "MH": "MAS", "AK": "AXM", "GA": "GIA", "AI": "AIC",
    "QF": "QFA", "JQ": "JST", "3K": "JSA", "NZ": "ANZ", "VA": "VOZ",
    # 北米・欧州・中東
    "AA": "AAL", "DL": "DAL", "UA": "UAL", "AC": "ACA", "HA": "HAL", "AS": "ASA",
    "BA": "BAW", "LH": "DLH", "AF": "AFR", "KL": "KLM", "AY": "FIN", "TK": "THY",
    "EK": "UAE", "QR": "QTR", "EY": "ETD",
    # 貨物
    "FX": "FDX", "5X": "UPS", "KZ": "NCA", "CV": "CLX", "5Y": "GTI", "K4": "CKS",
}
_IATA_FLIGHT_RE = re.compile(r"^([A-Z0-9]{2})(\d{1,4}[A-Z]?)$")

PREFIX = "!"
PAGE_SIZE = 20
HEX6 = re.compile(r"^[0-9a-fA-F]{6}$")
PRIORITIES = {"NORMAL", "WATCH", "SPECIAL"}
AIRPORT_SHORT_NAMES = {
    "NRT": "Narita", "HND": "Haneda", "NGO": "Chubu", "KIX": "Kansai",
    "ITM": "Itami", "CTS": "New Chitose", "FUK": "Fukuoka", "OKA": "Naha",
    "ICN": "Incheon", "SLC": "Salt Lake City",
}
_DURATION_RE = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)
_JA_REGISTRATION_RE = re.compile(r"(?<![A-Z0-9])JA[- ]?([0-9A-Z]{4})(?![A-Z0-9])", re.IGNORECASE)
_MILITARY_SERIAL_RE = re.compile(r"(?<![A-Z0-9])([0-9]{2,3}-[0-9]{4,6})(?![A-Z0-9])")
_US_N_NUMBER_RE = re.compile(r"(?<![A-Z0-9])(N[0-9]{1,5}[A-Z]{0,2})(?![A-Z0-9])", re.IGNORECASE)
_LABELED_REGISTRATION_RE = re.compile(
    r"(?:機体番号|登録記号|registration)\s*[:：]?\s*([A-Z0-9][A-Z0-9-]{2,9})",
    re.IGNORECASE,
)

USAGE = {
    "add": "!add <登録記号> [icao24] [機種]",
    "remove": "!remove <登録記号 または icao24>",
    "find": "!find <キーワード>",
    "list": "!list [ページ番号]",
    "flight": "!flight <便名 / コールサイン / 登録記号 / icao24>",
    "my-special-add": "!my-special-add <登録記号 または icao24>",
    "my-special-remove": "!my-special-remove <登録記号 または icao24>",
    "my-special-list": "!my-special-list",
    "my-special-settings": "!my-special-settings <on または off>",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aircraft-bot")

intents = discord.Intents.default()
intents.message_content = True


class AircraftBot(commands.Bot):
    async def setup_hook(self):
        # スラッシュコマンドはCloudflare Workerと worker/commands.json で管理する。
        # ここでtree.sync()すると、Python側に定義した一部コマンドだけで
        # /infoなどWorker専用コマンドを上書きしてしまうため同期しない。
        logger.info("スラッシュコマンドの登録はWorker側で管理します")


bot = AircraftBot(command_prefix=PREFIX, intents=intents)


# ============ watchlist の読み書き ============

def load_watchlist():
    with open(WATCHLIST_PATH, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def save_watchlist(data):
    tmp_path = WATCHLIST_PATH + ".tmp"
    with open(WATCHLIST_PATH, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            with open(tmp_path, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=4)
            os.replace(tmp_path, WATCHLIST_PATH)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_locked_json(path, default):
    """別プロセスと共有するJSONをロック付きで読み込む。"""
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


def save_locked_json(path, data):
    """別プロセスと共有するJSONをロック付きで安全に置き換える。"""
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


def consume_personal_special_events():
    """監視処理が作ったDM通知イベントをまとめて取り出す。"""
    path = PERSONAL_SPECIAL_EVENTS_PATH
    lock_path = path + ".lock"
    tmp_path = path + ".tmp"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            events = []
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as data_file:
                        events = json.load(data_file)
                except (OSError, ValueError):
                    events = []
            with open(tmp_path, "w", encoding="utf-8") as data_file:
                json.dump([], data_file)
            os.replace(tmp_path, path)
            return events if isinstance(events, list) else []
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def append_personal_special_events(events):
    """一時的な送信失敗イベントをキューへ戻す。"""
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


def normalize(value):
    """watchlistの値(文字列形式 or dict形式)を (label, type) にそろえる。"""
    if isinstance(value, dict):
        return value.get("label", "?"), value.get("type") or "不明"
    return str(value), "不明"


def normalize_priority(value):
    priority = str(value or "NORMAL").upper()
    return priority if priority in PRIORITIES else "NORMAL"


def effective_priority(value, now=None):
    """旧形式を含むwatchlist値の、期限を考慮した現在の優先度。"""
    if not isinstance(value, dict):
        return "NORMAL"
    priority = normalize_priority(value.get("priority"))
    if priority != "SPECIAL" or not value.get("special_until"):
        return priority
    try:
        expires_at = float(value["special_until"])
    except (TypeError, ValueError):
        return priority
    if (time.time() if now is None else now) < expires_at:
        return "SPECIAL"
    return normalize_priority(value.get("priority_after_special"))


def find_watchlist_entry(watchlist, aircraft):
    """icao24または登録記号の完全一致で (icao24, value) を返す。"""
    key = aircraft.strip().lower()
    if key in watchlist:
        return key, watchlist[key]
    target = aircraft.replace("-", "").replace(" ", "").upper()
    for icao24, value in watchlist.items():
        label, _ = normalize(value)
        if label.replace("-", "").replace(" ", "").upper() == target:
            return icao24, value
    return None


def as_entry(value, icao24):
    """文字列の旧形式も、追加情報を保持できるdict形式にする。"""
    if isinstance(value, dict):
        return dict(value)
    return {"label": str(value or icao24), "type": "不明"}


def clear_expired_special(entry, now=None):
    """期限切れSPECIALを元の優先度へ戻す。変更した場合はTrue。"""
    if not isinstance(entry, dict) or not entry.get("special_until"):
        return False
    try:
        expired = float(entry["special_until"]) <= (time.time() if now is None else now)
    except (TypeError, ValueError):
        expired = False
    if not expired:
        return False
    entry["priority"] = normalize_priority(entry.pop("priority_after_special", "NORMAL"))
    entry.pop("special_until", None)
    return True


def parse_duration(value):
    """24h / 30m / 7d を秒へ変換する。"""
    match = _DURATION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("期間は `30m`、`24h`、`7d` の形式で指定してください。")
    amount = int(match.group(1))
    multiplier = {"m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    seconds = amount * multiplier
    if amount < 1 or seconds > 365 * 86400:
        raise ValueError("期間は1分以上365日以内で指定してください。")
    return seconds


def parse_photo_post(content, created_at=None):
    """写真投稿の本文から登録記号・撮影場所・撮影日・感想を取り出す。"""
    content = (content or "").strip()
    ja_match = _JA_REGISTRATION_RE.search(content)
    military_match = _MILITARY_SERIAL_RE.search(content)
    n_number_match = _US_N_NUMBER_RE.search(content)
    labeled_match = _LABELED_REGISTRATION_RE.search(content)
    registration = None
    if ja_match:
        registration = f"JA{ja_match.group(1)}".upper()
    elif military_match:
        registration = military_match.group(1).upper()
    elif n_number_match:
        registration = n_number_match.group(1).upper()
    elif labeled_match:
        registration = labeled_match.group(1).upper()

    values = {}
    labels = {
        "撮影場所": "location", "場所": "location",
        "撮影日": "date", "日付": "date",
        "感想": "comment", "ひとこと": "comment", "コメント": "comment",
    }
    unlabelled = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched_label = False
        for label, key in labels.items():
            match = re.match(rf"^{label}\s*[:：]?\s*(.*)$", line, re.IGNORECASE)
            if match:
                values[key] = match.group(1).strip()
                matched_label = True
                break
        if matched_label or _LABELED_REGISTRATION_RE.search(line):
            continue
        if registration and _normalize_reg(line) == _normalize_reg(registration):
            continue
        unlabelled.append(line)

    if unlabelled and not values.get("location"):
        values["location"] = unlabelled.pop(0)
    if unlabelled and not values.get("comment"):
        values["comment"] = "\n".join(unlabelled)
    if not values.get("date"):
        stamp = created_at or datetime.now(timezone.utc)
        values["date"] = stamp.astimezone(timezone(timedelta(hours=9))).strftime("%Y年%-m月%-d日")
    return {
        "registration": registration,
        "location": values.get("location") or "未記入",
        "date": values["date"],
        "comment": values.get("comment") or "（感想なし）",
    }


def is_photo_attachment(attachment):
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    filename = (getattr(attachment, "filename", "") or "").lower()
    return content_type.startswith("image/") or filename.endswith(PHOTO_EXTENSIONS)


def lookup_photo_aircraft(registration):
    """写真投稿向けにicao24・機種・所有者情報をまとめて取得する。"""
    icao24 = lookup_icao24(registration)
    type_name = None
    operator = None
    canonical_reg = registration
    if icao24:
        try:
            response = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao24}", timeout=5)
            if response.status_code == 200:
                data = response.json()
                manufacturer = data.get("Manufacturer", "") or ""
                model = data.get("Type", "") or data.get("ICAOTypeCode", "") or ""
                type_name = f"{manufacturer} {model}".strip() or None
                operator = (
                    data.get("RegisteredOwners")
                    or data.get("RegisteredOwnerOperatorName")
                    or data.get("RegisteredOwnerOperator")
                    or data.get("RegisteredOwner")
                )
        except (requests.RequestException, ValueError) as exc:
            logger.warning("photo aircraft info lookup failed: %s", exc)
    if not icao24 or not type_name:
        found = lookup_from_tar1090(registration)
        if found:
            found_icao24, found_type, canonical_reg = found
            icao24 = icao24 or found_icao24
            type_name = type_name or found_type
    return {
        "registration": canonical_reg,
        "icao24": icao24 or "不明",
        "type": type_name or "不明",
        "operator": operator or "不明",
    }


# ============ 検索(すべて同期関数。呼び出し側で to_thread する) ============

def lookup_icao24(registration: str):
    """登録記号(例: JA08XJ)からicao24を hexdb.io で検索する"""
    try:
        r = requests.get(
            f"https://hexdb.io/api/v1/aircraft/reg-icao/{registration}", timeout=5
        )
        text = r.text.strip()
        if r.status_code == 200 and text and "error" not in text.lower():
            return text.lower()
    except requests.RequestException as e:
        logger.warning(f"reg-icao lookup failed: {e}")
    return None


def lookup_aircraft_type(icao24: str):
    """icao24から機種情報を hexdb.io で検索する"""
    try:
        r = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao24}", timeout=5)
        if r.status_code == 200:
            data = r.json()
            manufacturer = data.get("Manufacturer", "") or ""
            type_code = data.get("Type", "") or data.get("ICAOTypeCode", "") or ""
            full_type = f"{manufacturer} {type_code}".strip()
            return full_type or None
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"aircraft info lookup failed: {e}")
    return None


def lookup_from_local_db(registration: str):
    """ローカルの aircraftDatabase.csv から (icao24, 機種) を探す。なければNone。"""
    if not os.path.exists(AIRCRAFT_DB_PATH):
        return None
    target = registration.upper()
    try:
        with open(AIRCRAFT_DB_PATH, "r", encoding="utf-8", errors="replace", newline="") as f:
            header = f.readline()
            # 古い版は 'icao24','registration',... とシングルクォートで囲まれている
            quote = "'" if header.lstrip().startswith("'") else '"'
            f.seek(0)
            for row in csv.DictReader(f, quotechar=quote):
                if (row.get("registration") or "").strip().upper() != target:
                    continue
                icao24 = (row.get("icao24") or "").strip().lower()
                if not icao24:
                    continue
                maker = (row.get("manufacturername") or "").strip()
                model = (row.get("model") or "").strip()
                return icao24, (f"{maker} {model}".strip() or None)
    except (OSError, csv.Error) as e:
        logger.warning(f"local DB lookup failed: {e}")
    return None


def _normalize_reg(reg: str) -> str:
    return reg.replace("-", "").replace(" ", "").upper()


def _download_tar1090_db() -> bool:
    """tar1090-db を取得して置き換える。失敗しても既存ファイルは残す。"""
    tmp_path = TAR1090_DB_PATH + ".tmp"
    try:
        with requests.get(TAR1090_DB_URL, timeout=60, stream=True) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        os.replace(tmp_path, TAR1090_DB_PATH)
        logger.info("tar1090-db を更新しました")
        return True
    except (requests.RequestException, OSError) as e:
        logger.warning(f"tar1090-db download failed: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def _scan_tar1090_db(registration: str):
    target = _normalize_reg(registration)
    try:
        with gzip.open(TAR1090_DB_PATH, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                # 形式: icao24;登録記号;ICAO機種コード;フラグ;機種名;...
                parts = line.split(";", 5)
                if len(parts) < 5 or _normalize_reg(parts[1]) != target:
                    continue
                icao24 = parts[0].strip().lower()
                canonical_reg = parts[1].strip()
                typecode = parts[2].strip()
                desc = parts[4].strip()
                if desc and typecode:
                    type_name = f"{desc} ({typecode})"
                else:
                    type_name = desc or typecode or None
                return icao24, type_name, canonical_reg
    except (OSError, EOFError) as e:
        logger.warning(f"tar1090-db scan failed: {e}")
    return None


def lookup_from_tar1090(registration: str):
    """(icao24, 機種, 正式な登録記号) を返す。なければNone。DBは初回に自動ダウンロードする。"""
    if not os.path.exists(TAR1090_DB_PATH) and not _download_tar1090_db():
        return None
    result = _scan_tar1090_db(registration)
    if result is None:
        age = time.time() - os.path.getmtime(TAR1090_DB_PATH)
        if age > TAR1090_REFRESH_SECONDS and _download_tar1090_db():
            result = _scan_tar1090_db(registration)
    return result


def resolve_personal_aircraft(aircraft):
    """個人SPECIAL用に登録記号/icao24を (icao24, label, type) へ解決する。"""
    query = aircraft.strip().upper()
    watchlist = load_watchlist()
    found = find_watchlist_entry(watchlist, query)
    if found:
        icao24, value = found
        label, type_name = normalize(value)
        return icao24, label, type_name

    if HEX6.fullmatch(query):
        return query.lower(), query.lower(), lookup_aircraft_type(query.lower()) or "不明"

    icao24 = lookup_icao24(query)
    type_name = None
    label = query
    if icao24:
        type_name = lookup_aircraft_type(icao24)
    if not icao24 or not type_name:
        tar_result = lookup_from_tar1090(query)
        if tar_result:
            tar_icao24, tar_type, canonical_reg = tar_result
            icao24 = icao24 or tar_icao24
            type_name = type_name or tar_type
            label = canonical_reg or label
    if not icao24:
        local_result = lookup_from_local_db(query)
        if local_result:
            icao24, local_type = local_result
            type_name = type_name or local_type
    if not icao24:
        return None
    return icao24.lower(), label, type_name or "不明"


# ============ 現在位置の検索(!flight) ============

_COMPASS = ["北", "北東", "東", "南東", "南", "南西", "西", "北西"]


def compass(deg):
    return _COMPASS[int((deg + 22.5) // 45) % 8]


def format_airport(airport):
    iata = (airport.get("iata_code") or "").upper()
    code = iata or airport.get("icao_code") or "?"
    name = AIRPORT_SHORT_NAMES.get(iata) or airport.get("name") or airport.get("municipality") or ""
    return f"{name} ({code})" if name else code


def fetch_route(callsign):
    """adsbdb から、コールサインの予定ルート(出発地・到着地)を取得する。なければNone。"""
    callsign = (callsign or "").strip()
    if not callsign:
        return None
    try:
        r = requests.get(
            f"https://api.adsbdb.com/v0/callsign/{callsign}",
            timeout=5, headers=HTTP_HEADERS,
        )
        if r.status_code != 200:
            return None
        body = r.json().get("response")
        fr = body.get("flightroute") if isinstance(body, dict) else None
        if not fr or not fr.get("origin") or not fr.get("destination"):
            return None
        route = {
            "origin": fr["origin"],
            "destination": fr["destination"],
            "flight_iata": fr.get("callsign_iata"),
        }
        return correct_route(callsign, route)
    except (requests.RequestException, ValueError, AttributeError):
        return None


def callsign_candidates(text: str):
    """入力から、検索するコールサインの候補を優先順に作る。JL123 → [JAL123, JL123]"""
    t = re.sub(r"[\s\-]", "", text.upper())
    candidates = []
    m = _IATA_FLIGHT_RE.match(t)
    if m and m.group(1) in IATA_TO_ICAO:
        candidates.append(IATA_TO_ICAO[m.group(1)] + m.group(2))
    candidates.append(t)
    return list(dict.fromkeys(c for c in candidates if c))


def adsb_lookup(kind: str, value: str):
    """kind は callsign / hex。見つかった機体(dict)のリストを返す。なければ[]。"""
    for base in ADSB_API_BASES:
        try:
            r = requests.get(
                f"{base}/v2/{kind}/{value}", timeout=ADSB_TIMEOUT, headers=HTTP_HEADERS
            )
            if r.status_code != 200:
                continue
            aircraft = r.json().get("ac") or []
            if aircraft:
                return aircraft
        except (requests.RequestException, ValueError):
            continue
    return []


def find_live_aircraft(text: str):
    """便名・コールサイン・登録記号・icao24のどれかから、今飛んでいる機体を探す。"""
    for callsign in callsign_candidates(text):
        found = adsb_lookup("callsign", callsign)
        if found:
            return found

    compact = re.sub(r"[\s\-]", "", text)
    if HEX6.match(compact):
        found = adsb_lookup("hex", compact.lower())
        if found:
            return found

    # 登録記号として、tar1090-db で icao24 に変換してから探す
    resolved = lookup_from_tar1090(text)
    if resolved:
        return adsb_lookup("hex", resolved[0])
    return []


def build_flight_embed(ac, route=None):
    callsign = (ac.get("flight") or "").strip()
    hex_id = (ac.get("hex") or "").lower()
    reg = ac.get("r")
    alt = ac.get("alt_baro")
    on_ground = alt == "ground"

    embed = discord.Embed(
        title=f"✈️ {callsign or hex_id}" + (f" ({reg})" if reg else ""),
        url=f"https://globe.adsbexchange.com/?icao={hex_id}",
        description="**地上**" if on_ground else "**飛行中**",
        color=0x2ECC71 if on_ground else 0x3498DB,
    )
    embed.add_field(name="機種", value=ac.get("desc") or ac.get("t") or "不明", inline=True)
    embed.add_field(name="登録記号", value=f"`{reg}`" if reg else "不明", inline=True)
    embed.add_field(name="icao24", value=f"`{hex_id}`", inline=True)

    if route:
        flight = f"{route['flight_iata']} · " if route.get("flight_iata") else ""
        embed.add_field(
            name="区間(参考・一致未確認)",
            value=f"{flight}{format_airport(route['origin'])} → {format_airport(route['destination'])}",
            inline=False,
        )

    if not on_ground:
        if isinstance(alt, (int, float)):
            embed.add_field(name="高度", value=f"{alt:,.0f} ft ({alt * 0.3048:,.0f} m)", inline=True)
        gs = ac.get("gs")
        if isinstance(gs, (int, float)):
            embed.add_field(name="速度", value=f"{gs * 1.852:,.0f} km/h ({gs:,.0f} kt)", inline=True)
        track = ac.get("track")
        if isinstance(track, (int, float)):
            embed.add_field(name="進行方向", value=f"{compass(track)} ({track:.0f}°)", inline=True)
        rate = ac.get("baro_rate")
        if isinstance(rate, (int, float)) and abs(rate) >= 64:
            arrow = "↑ 上昇" if rate > 0 else "↓ 降下"
            embed.add_field(name="垂直速度", value=f"{arrow} {abs(rate):,.0f} ft/min", inline=True)

    lat, lon = ac.get("lat"), ac.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        embed.add_field(name="位置", value=f"{lat:.3f}, {lon:.3f}", inline=True)
    else:
        embed.add_field(name="位置", value="位置情報なし", inline=True)

    seen_pos = ac.get("seen_pos")
    if isinstance(seen_pos, (int, float)) and seen_pos > 60:
        embed.add_field(name="最終位置受信", value=f"{seen_pos:.0f}秒前", inline=True)

    footer = "ADS-B: adsb.lol / adsb.one · タイトルをタップで地図"
    if reg:
        footer += f" · 監視に追加: !add {reg}"
    embed.set_footer(text=footer)
    return embed


# ============ 取りこぼし対策 ============

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): int(v) for k, v in data.get("last_ids", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(ids):
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"last_ids": ids}, f)
    os.replace(tmp_path, STATE_PATH)


last_ids = load_state()
catchup_lock = asyncio.Lock()


def mark_handled(channel_id, message_id) -> bool:
    """未処理のコマンドならTrueを返して記録する。処理済み(ID <= 記録済み)ならFalse。
    リアルタイム受信・再接続後の拾い直し・Discordのイベント再送のどれで来ても、二重実行しない。"""
    key = str(channel_id)
    if message_id <= last_ids.get(key, 0):
        return False
    last_ids[key] = message_id
    try:
        save_state(last_ids)
    except OSError as e:
        logger.warning(f"state save failed: {e}")
    return True


async def catch_up_missed_commands():
    """切断中に送られたコマンドや質問箱の投稿を拾って処理する。"""
    async with catchup_lock:
        cutoff = datetime.now(timezone.utc) - CATCHUP_MAX_AGE
        for key, last_id in list(last_ids.items()):
            channel = bot.get_channel(int(key))
            if channel is None:
                continue
            try:
                async for msg in channel.history(
                    limit=CATCHUP_LIMIT, after=discord.Object(id=last_id), oldest_first=True
                ):
                    if msg.author.bot:
                        continue
                    is_feedback = (
                        msg.channel.id == FEEDBACK_SOURCE_CHANNEL_ID
                        and msg.content.strip().startswith(FEEDBACK_MARKER)
                    )
                    if not is_feedback and not msg.content.startswith(PREFIX):
                        continue
                    if not mark_handled(msg.channel.id, msg.id):
                        continue
                    if msg.created_at < cutoff:
                        logger.info(f"古いコマンドはスキップ: {msg.content!r}")
                        continue
                    if is_feedback:
                        await notify_feedback(msg)
                        continue
                    logger.info(f"取りこぼしコマンドを処理: {msg.content!r}")
                    try:
                        await msg.add_reaction("⏱️")
                    except discord.HTTPException:
                        pass
                    await bot.process_commands(msg)
            except discord.HTTPException as e:
                logger.warning(f"catch-up failed for channel {key}: {e}")


@tasks.loop(seconds=HEARTBEAT_INTERVAL)
async def heartbeat():
    """Discordに接続できている間だけ、生存の印を更新する。"""
    if bot.is_ready() and not bot.is_closed() and math.isfinite(bot.latency):
        try:
            with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except OSError as e:
            logger.warning(f"heartbeat write failed: {e}")


@tasks.loop(seconds=10)
async def personal_special_dispatch():
    """監視処理が検出した個人SPECIALを、登録者本人へDMする。"""
    if not bot.is_ready() or bot.is_closed():
        return
    events = await asyncio.to_thread(consume_personal_special_events)
    retry_events = []
    for event in events:
        try:
            user_id = int(event["user_id"])
            recipient = await bot.fetch_user(user_id)
            state = "引き続き検出しています" if event.get("repeat") else "新たに検出しました"
            embed = discord.Embed(
                title=f"🚨 個人SPECIAL｜{event['label']}を検出",
                description=f"{event['region']}の監視範囲内で{state}。",
                color=0xED4245,
                timestamp=datetime.fromtimestamp(
                    float(event.get("detected_at", time.time())), timezone.utc
                ),
                url=f"https://globe.adsbexchange.com/?icao={event['icao24']}",
            )
            embed.add_field(name="機種", value=event.get("type") or "不明", inline=True)
            embed.add_field(
                name="コールサイン",
                value=f"`{event.get('callsign') or '不明'}`",
                inline=True,
            )
            embed.add_field(name="ICAO24", value=f"`{event['icao24']}`", inline=True)
            if event.get("altitude") is not None and not event.get("on_ground"):
                altitude = float(event["altitude"])
                embed.add_field(
                    name="高度",
                    value=f"{altitude:,.0f} m ({altitude * 3.28084:,.0f} ft)",
                    inline=True,
                )
            if event.get("velocity") is not None and not event.get("on_ground"):
                velocity = float(event["velocity"])
                embed.add_field(
                    name="速度",
                    value=f"{velocity * 3.6:,.0f} km/h ({velocity * 1.94384:,.0f} kt)",
                    inline=True,
                )
            embed.set_footer(text="タイトルをタップするとADS-B Exchangeを開きます")
            await recipient.send(embed=embed)
        except discord.Forbidden:
            logger.warning("個人SPECIALのDMを送信できません: user=%s", event.get("user_id"))
        except (discord.HTTPException, KeyError, TypeError, ValueError) as exc:
            logger.error("個人SPECIALのDM送信に失敗しました: %s", exc)
            retry_count = int(event.get("retry_count", 0)) + 1
            if retry_count <= 3:
                event["retry_count"] = retry_count
                retry_events.append(event)
    if retry_events:
        await asyncio.to_thread(append_personal_special_events, retry_events)


# ============ イベント ============

async def notify_feedback(message):
    """質問箱への通常投稿を管理者専用チャンネルへ転送し、元投稿を消す。"""
    if message.author.id == FEEDBACK_OWNER_ID:
        return
    destination = bot.get_channel(FEEDBACK_DESTINATION_CHANNEL_ID)
    if destination is None:
        try:
            destination = await bot.fetch_channel(FEEDBACK_DESTINATION_CHANNEL_ID)
        except discord.HTTPException as exc:
            logger.error("質問箱の転送先を取得できません: %s", exc)
            return
    allowed = discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=FEEDBACK_OWNER_ID)],
        replied_user=False,
    )
    attachments = "\n".join(attachment.url for attachment in message.attachments)
    description = message.content.strip()[len(FEEDBACK_MARKER):].strip()
    description = description or "（本文なし・添付ファイルのみ）"
    if attachments:
        description += f"\n\n**添付ファイル**\n{attachments}"
    embed = discord.Embed(
        title="📮 新しい質問・改善要望",
        description=description[:4000],
        color=0x5865F2,
        timestamp=message.created_at,
    )
    embed.add_field(
        name="送信者",
        value=f"{message.author.mention} (`{message.author.id}`)",
        inline=False,
    )
    try:
        await destination.send(
            f"<@{FEEDBACK_OWNER_ID}>", embed=embed, allowed_mentions=allowed
        )
    except discord.HTTPException as exc:
        logger.error("質問箱の転送に失敗しました: %s", exc)
        return
    try:
        await message.delete()
    except discord.HTTPException as exc:
        logger.warning("転送後の元投稿を削除できませんでした: %s", exc)


async def answer_feedback(message):
    """管理者が転送メッセージへ返信した内容を、元の質問者へDMする。"""
    reference = message.reference
    if reference is None or reference.message_id is None:
        return False
    forwarded = reference.resolved if isinstance(reference.resolved, discord.Message) else None
    if forwarded is None:
        try:
            forwarded = await message.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            return False
    if forwarded.author.id != bot.user.id or not forwarded.embeds:
        return False

    sender_field = next(
        (field for field in forwarded.embeds[0].fields if field.name == "送信者"), None
    )
    match = re.search(r"\b(\d{17,20})\b", sender_field.value if sender_field else "")
    if match is None:
        return False

    answer = message.content.strip()
    attachment_urls = "\n".join(item.url for item in message.attachments)
    if attachment_urls:
        answer = f"{answer}\n\n添付ファイル:\n{attachment_urls}".strip()
    if not answer:
        await message.reply("⚠️ 回答内容を入力してください。", mention_author=False)
        return True

    try:
        recipient = await bot.fetch_user(int(match.group(1)))
        question = forwarded.embeds[0].description or "（質問内容なし）"
        embed = discord.Embed(
            title=f"📬 {message.author.display_name}から回答が届きました",
            description="質問・改善要望BOXへの返信です。",
            color=0x57F287,
            timestamp=message.created_at,
        )
        embed.add_field(name="💬 回答", value=answer[:1024], inline=False)
        embed.add_field(
            name="📮 あなたが送った質問・要望",
            value=question[:1024],
            inline=False,
        )
        embed.set_footer(
            text="追加で質問する場合は、bot-commandsで先頭に📮を付けて投稿してください。"
        )
        await recipient.send(embed=embed)
        await message.add_reaction("✅")
    except discord.Forbidden:
        await message.reply(
            "⚠️ 質問者がDMを拒否しているため、回答を送信できませんでした。",
            mention_author=False,
        )
    except discord.HTTPException as exc:
        logger.error("質問箱の回答送信に失敗しました: %s", exc)
        await message.reply("⚠️ 回答の送信に失敗しました。", mention_author=False)
    return True


async def handle_photo_post(message):
    """航空機写真を確認し、機体情報付きのスレッドへ整理する。"""
    photos = [item for item in message.attachments if is_photo_attachment(item)]
    if not photos:
        await message.reply(
            "📸 写真を添付し、本文の最初に機体番号を書いてください。\n"
            "例: `JA784A` → `成田空港` → `夕日がきれいでした！`",
            mention_author=False,
            delete_after=30,
        )
        return

    post = parse_photo_post(message.content, message.created_at)
    if not post["registration"]:
        await message.reply(
            "⚠️ 機体番号を読み取れませんでした。本文に `JA784A` のような登録記号を入れてください。",
            mention_author=False,
        )
        return

    async with message.channel.typing():
        aircraft = await asyncio.to_thread(
            lookup_photo_aircraft, post["registration"]
        )

    registration = aircraft["registration"]
    location = post["location"]
    title = f"✈️ {registration}｜{location}"
    embed = discord.Embed(
        title=title[:256],
        description=post["comment"][:4096],
        color=0x3498DB,
        timestamp=message.created_at,
    )
    embed.add_field(name="📷 撮影場所", value=location[:1024], inline=True)
    embed.add_field(name="📅 撮影日", value=post["date"][:1024], inline=True)
    embed.add_field(name="🛩️ 機種", value=aircraft["type"][:1024], inline=False)
    embed.add_field(name="🏢 所有者・運航会社", value=aircraft["operator"][:1024], inline=False)
    embed.add_field(name="🔎 ICAO24", value=f"`{aircraft['icao24']}`", inline=True)
    embed.set_author(
        name=f"{message.author.display_name}さんの投稿",
        icon_url=message.author.display_avatar.url,
    )
    embed.set_image(url=photos[0].url)
    embed.set_footer(text="このスレッドで写真へのコメントや情報交換ができます。")

    thread_name = f"{registration}｜{location}"[:100]
    try:
        thread = await message.create_thread(name=thread_name, auto_archive_duration=1440)
        await thread.send(embed=embed)
        await message.add_reaction("✈️")
    except discord.Forbidden:
        logger.warning("写真投稿スレッドの作成権限がありません")
        await message.reply(
            "⚠️ Botに「公開スレッドを作成」と「スレッドでメッセージを送信」の権限が必要です。",
            mention_author=False,
        )
    except discord.HTTPException as exc:
        logger.error("写真投稿の整理に失敗しました: %s", exc)
        await message.reply("⚠️ 写真の整理に失敗しました。少し待ってから再投稿してください。", mention_author=False)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    if not heartbeat.is_running():
        heartbeat.start()
    if not personal_special_dispatch.is_running():
        personal_special_dispatch.start()
    await catch_up_missed_commands()


@bot.event
async def on_resumed():
    await catch_up_missed_commands()


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id == PHOTO_CHANNEL_ID:
        await handle_photo_post(message)
        return
    if (
        message.channel.id == FEEDBACK_DESTINATION_CHANNEL_ID
        and message.author.id == FEEDBACK_OWNER_ID
        and await answer_feedback(message)
    ):
        return
    if (
        message.channel.id == FEEDBACK_SOURCE_CHANNEL_ID
        and message.content.strip().startswith(FEEDBACK_MARKER)
    ):
        if mark_handled(message.channel.id, message.id):
            await notify_feedback(message)
        return
    if message.content.startswith(PREFIX) and not mark_handled(message.channel.id, message.id):
        return  # すでに処理済み(再接続後の拾い直しなど)
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⛔ このコマンドはサーバー管理者だけが使用できます。")
        return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        usage = USAGE.get(ctx.command.name, f"!{ctx.command.name}")
        await ctx.send(f"⚠️ 使い方: `{usage}`")
        return
    logger.error(f"command error in {ctx.command}: {error!r}", exc_info=error)


# ============ コマンド ============

@bot.command(name="add")
@commands.has_permissions(administrator=True)
async def add_aircraft(ctx, tail: str, icao24: str = None, *, type_name: str = None):
    tail = tail.upper()

    # 「!add JA08XJ Boeing 777」のように icao24 を飛ばして機種だけ書かれた場合
    if icao24 is not None and not HEX6.match(icao24):
        type_name = f"{icao24} {type_name}".strip() if type_name else icao24
        icao24 = None

    async with ctx.typing():
        db_type = None
        if icao24 is None:
            icao24 = await asyncio.to_thread(lookup_icao24, tail)
            if icao24 is None:
                found = await asyncio.to_thread(lookup_from_tar1090, tail)
                if found:
                    icao24, db_type, tail = found  # 登録記号は正式表記(HS-TYV)にそろえる
            if icao24 is None:
                found = await asyncio.to_thread(lookup_from_local_db, tail)
                if found:
                    icao24, db_type = found

        if icao24 is None:
            await ctx.send(
                f"⚠️ `{tail}` のicao24が自動取得できませんでした。"
                f"`!add {tail} <icao24> [機種]` の形で手動指定してください。"
            )
            return
        icao24 = icao24.lower()

        if type_name is None:
            type_name = (
                await asyncio.to_thread(lookup_aircraft_type, icao24)
                or db_type
                or "不明"
            )

    watchlist = load_watchlist()
    if icao24 in watchlist:
        label, _ = normalize(watchlist[icao24])
        await ctx.send(f"ℹ️ `{label}` ({icao24}) は既に登録済みです。")
        return

    watchlist[icao24] = {"label": tail, "type": type_name, "priority": "NORMAL"}
    save_watchlist(watchlist)
    await ctx.send(f"✅ `{tail}` ({icao24} / {type_name}) をwatchlistに追加しました。")


@bot.command(name="remove")
@commands.has_permissions(administrator=True)
async def remove_aircraft(ctx, tail_or_icao: str):
    key = tail_or_icao.lower()
    watchlist = load_watchlist()

    if key in watchlist:
        label, _ = normalize(watchlist.pop(key))
        save_watchlist(watchlist)
        await ctx.send(f"🗑️ `{label}` ({key}) をwatchlistから削除しました。")
        return

    for icao24, value in list(watchlist.items()):
        label, _ = normalize(value)
        if label.upper() == tail_or_icao.upper():
            watchlist.pop(icao24)
            save_watchlist(watchlist)
            await ctx.send(f"🗑️ `{label}` ({icao24}) をwatchlistから削除しました。")
            return

    await ctx.send(f"⚠️ `{tail_or_icao}` はwatchlistに見つかりませんでした。")


@bot.command(name="find")
async def find_aircraft(ctx, keyword: str):
    watchlist = load_watchlist()
    keyword_upper = keyword.upper()
    matches = []
    for icao24, value in watchlist.items():
        label, type_name = normalize(value)
        if keyword_upper in label.upper() or keyword_upper in icao24.upper():
            matches.append(
                f"`{label}` ({icao24} / {type_name} / {effective_priority(value)})"
            )

    if not matches:
        await ctx.send(f"「{keyword}」に一致する機体はありません。")
        return

    text = "\n".join(matches[:PAGE_SIZE])
    if len(matches) > PAGE_SIZE:
        text += f"\n…他 {len(matches) - PAGE_SIZE} 件(キーワードを絞ってください)"
    await ctx.send(text)


@bot.command(name="list")
async def list_aircraft(ctx, page: int = 1):
    watchlist = load_watchlist()
    if not watchlist:
        await ctx.send("watchlistは空です。")
        return

    items = sorted(
        ((*normalize(v), icao24) for icao24, v in watchlist.items()),
        key=lambda x: x[0].upper(),
    )
    total = len(items)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, pages))
    chunk = items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]

    lines = [
        f"`{label}` ({icao24} / {type_name} / {effective_priority(watchlist[icao24])})"
        for label, type_name, icao24 in chunk
    ]
    header = f"📋 watchlist 全{total}機(ページ {page}/{pages})"
    footer = f"\n次のページ: `!list {page + 1}`" if page < pages else ""
    await ctx.send((header + "\n" + "\n".join(lines) + footer)[:1990])


@bot.command(name="flight", aliases=["fl"])
async def flight_lookup(ctx, *, query: str):
    async with ctx.typing():
        aircraft_list = await asyncio.to_thread(find_live_aircraft, query)
        if not aircraft_list:
            await ctx.send(
                f"❓ 「{query}」に一致する機体は、いまのADS-Bでは見つかりませんでした。"
                "離陸前・着陸後、受信範囲外、または便名とコールサインが違う便の可能性があります"
                "(`!flight JAL123` のコールサインや、`!flight HS-TYV` の登録記号でも試せます)。"
            )
            return
        embeds = []
        for ac in aircraft_list[:3]:
            route = await asyncio.to_thread(fetch_route, ac.get("flight"))
            embeds.append(build_flight_embed(ac, route))
    await ctx.send(embeds=embeds)


async def require_personal_special_dm(ctx):
    """個人SPECIALの内容がサーバーへ出ないよう、DMからの利用だけを許可する。"""
    if ctx.guild is None:
        return True
    await ctx.send(
        "🔒 個人SPECIALは登録内容を非公開にするため、BotへのDMで使用してください。",
        delete_after=30,
    )
    return False


@bot.command(name="my-special")
async def my_special_help(ctx):
    if not await require_personal_special_dm(ctx):
        return
    await ctx.send(
        "🚨 **個人SPECIALの使い方**\n"
        "`!my-special-add JA784A` — 機体を追加\n"
        "`!my-special-remove JA784A` — 機体を解除\n"
        "`!my-special-list` — 自分の登録一覧\n"
        "`!my-special-settings on` — DM通知をON\n"
        "`!my-special-settings off` — DM通知をOFF\n\n"
        "登録内容はほかのメンバーには表示されません。"
    )


@bot.command(name="my-special-add")
async def my_special_add(ctx, aircraft: str):
    if not await require_personal_special_dm(ctx):
        return
    user_id = str(ctx.author.id)
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(user_id) or {"enabled": True, "aircraft": {}}
    registered = config.get("aircraft") or {}
    if len(registered) >= PERSONAL_SPECIAL_LIMIT:
        await ctx.send(f"⚠️ 個人SPECIALは1人{PERSONAL_SPECIAL_LIMIT}機まで登録できます。")
        return

    async with ctx.typing():
        result = await asyncio.to_thread(resolve_personal_aircraft, aircraft)
    if result is None:
        await ctx.send(
            f"⚠️ `{aircraft}` のICAO24を確認できませんでした。"
            "登録記号または6桁のICAO24を確認してください。"
        )
        return
    icao24, label, type_name = result
    if icao24 in registered:
        await ctx.send(f"ℹ️ `{label}` ({icao24}) はすでに個人SPECIALへ登録されています。")
        return
    registered[icao24] = {"label": label, "type": type_name}
    config["aircraft"] = registered
    config.setdefault("enabled", True)
    settings[user_id] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(
        f"✅ `{label}` ({icao24} / {type_name}) を個人SPECIALへ追加しました。\n"
        "日本国内で検出すると、ここへDMで通知します。"
    )


@bot.command(name="my-special-remove")
async def my_special_remove(ctx, aircraft: str):
    if not await require_personal_special_dm(ctx):
        return
    user_id = str(ctx.author.id)
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(user_id) or {"enabled": True, "aircraft": {}}
    registered = config.get("aircraft") or {}
    target = _normalize_reg(aircraft)
    match = next(
        (
            icao24 for icao24, entry in registered.items()
            if icao24.lower() == aircraft.lower()
            or _normalize_reg(entry.get("label", "")) == target
        ),
        None,
    )
    if match is None:
        await ctx.send(f"⚠️ `{aircraft}` は自分の個人SPECIALに登録されていません。")
        return
    removed = registered.pop(match)
    config["aircraft"] = registered
    settings[user_id] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(f"🗑️ `{removed.get('label', match)}` ({match}) を個人SPECIALから解除しました。")


@bot.command(name="my-special-list")
async def my_special_list(ctx):
    if not await require_personal_special_dm(ctx):
        return
    config = load_locked_json(PERSONAL_SPECIALS_PATH, {}).get(str(ctx.author.id)) or {}
    registered = config.get("aircraft") or {}
    state = "ON" if config.get("enabled", True) else "OFF"
    if not registered:
        await ctx.send(
            f"🚨 **自分の個人SPECIAL**（DM通知: {state}）\n登録機はありません。\n"
            "`!my-special-add JA784A` で追加できます。"
        )
        return
    lines = [
        f"`{entry.get('label', icao24)}` ({icao24} / {entry.get('type') or '不明'})"
        for icao24, entry in sorted(registered.items(), key=lambda item: item[1].get("label", ""))
    ]
    await ctx.send(
        (f"🚨 **自分の個人SPECIAL**（DM通知: {state} / {len(lines)}機）\n" + "\n".join(lines))[:1990]
    )


@bot.command(name="my-special-settings")
async def my_special_settings(ctx, enabled: str):
    if not await require_personal_special_dm(ctx):
        return
    normalized = enabled.strip().lower()
    if normalized not in {"on", "off"}:
        await ctx.send("⚠️ `!my-special-settings on` または `off` と入力してください。")
        return
    user_id = str(ctx.author.id)
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(user_id) or {"aircraft": {}}
    config["enabled"] = normalized == "on"
    settings[user_id] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(f"✅ 個人SPECIALのDM通知を **{normalized.upper()}** にしました。")


# ============ 管理者用スラッシュコマンド ============

async def require_administrator(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if interaction.guild is None or permissions is None or not permissions.administrator:
        await interaction.response.send_message(
            "⛔ このコマンドはサーバー管理者だけが使用できます。", ephemeral=True
        )
        return False
    return True


@bot.tree.command(name="priority", description="登録機の通知レベルを変更します(管理者専用)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(aircraft="登録記号またはicao24", level="通知レベル")
@app_commands.choices(level=[
    app_commands.Choice(name="NORMAL", value="NORMAL"),
    app_commands.Choice(name="WATCH", value="WATCH"),
    app_commands.Choice(name="SPECIAL", value="SPECIAL"),
])
async def priority_command(
    interaction: discord.Interaction,
    aircraft: str,
    level: app_commands.Choice[str],
):
    if not await require_administrator(interaction):
        return
    watchlist = load_watchlist()
    found = find_watchlist_entry(watchlist, aircraft)
    if not found:
        await interaction.response.send_message(
            f"⚠️ `{aircraft}` はwatchlistに見つかりませんでした。", ephemeral=True
        )
        return
    icao24, value = found
    entry = as_entry(value, icao24)
    entry["priority"] = level.value
    entry.pop("special_until", None)
    entry.pop("priority_after_special", None)
    watchlist[icao24] = entry
    save_watchlist(watchlist)
    label, _ = normalize(entry)
    await interaction.response.send_message(
        f"✅ `{label}` ({icao24}) を **{level.value}** に設定しました。", ephemeral=True
    )


@bot.tree.command(name="special", description="登録機を期限付きSPECIALにします(管理者専用)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(aircraft="登録記号またはicao24", duration="期間。例: 24h / 30m / 7d")
async def special_command(
    interaction: discord.Interaction,
    aircraft: str,
    duration: str = "24h",
):
    if not await require_administrator(interaction):
        return
    try:
        seconds = parse_duration(duration)
    except ValueError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
        return
    watchlist = load_watchlist()
    found = find_watchlist_entry(watchlist, aircraft)
    if not found:
        await interaction.response.send_message(
            f"⚠️ `{aircraft}` はwatchlistに見つかりませんでした。", ephemeral=True
        )
        return
    icao24, value = found
    entry = as_entry(value, icao24)
    previous = (
        entry.get("priority_after_special")
        if effective_priority(entry) == "SPECIAL"
        else effective_priority(entry)
    )
    entry["priority"] = "SPECIAL"
    entry["priority_after_special"] = normalize_priority(previous)
    entry["special_until"] = time.time() + seconds
    watchlist[icao24] = entry
    save_watchlist(watchlist)
    label, _ = normalize(entry)
    expires = int(entry["special_until"])
    await interaction.response.send_message(
        f"🚨 `{label}` ({icao24}) を <t:{expires}:F> まで **SPECIAL** に設定しました。",
        ephemeral=True,
    )


@bot.tree.command(name="nationwide", description="登録機の全国通知を切り替えます(管理者専用)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(aircraft="登録記号またはicao24", enabled="全国通知を有効にするか")
async def nationwide_command(
    interaction: discord.Interaction,
    aircraft: str,
    enabled: bool,
):
    if not await require_administrator(interaction):
        return
    watchlist = load_watchlist()
    found = find_watchlist_entry(watchlist, aircraft)
    if not found:
        await interaction.response.send_message(
            f"⚠️ `{aircraft}` はwatchlistに見つかりませんでした。", ephemeral=True
        )
        return
    icao24, value = found
    entry = as_entry(value, icao24)
    if enabled:
        entry["nationwide_alert"] = True
    else:
        entry.pop("nationwide_alert", None)
    watchlist[icao24] = entry
    save_watchlist(watchlist)
    label, _ = normalize(entry)
    state = "ON" if enabled else "OFF"
    await interaction.response.send_message(
        f"🗾 `{label}` ({icao24}) の全国通知を **{state}** にしました。",
        ephemeral=True,
    )


@bot.tree.command(name="special-list", description="SPECIAL登録機の一覧を表示します(管理者専用)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def special_list_command(interaction: discord.Interaction):
    if not await require_administrator(interaction):
        return
    watchlist = load_watchlist()
    changed = False
    specials = []
    for icao24, value in watchlist.items():
        entry = as_entry(value, icao24)
        if clear_expired_special(entry):
            watchlist[icao24] = entry
            changed = True
        if effective_priority(entry) != "SPECIAL":
            continue
        label, type_name = normalize(entry)
        until = entry.get("special_until")
        expiry = f" · <t:{int(float(until))}:R>まで" if until else " · 期限なし"
        specials.append(f"`{label}` ({icao24} / {type_name}){expiry}")
    if changed:
        save_watchlist(watchlist)
    if not specials:
        message = "SPECIAL登録機はありません。"
    else:
        message = "🚨 **SPECIAL登録機**\n" + "\n".join(specials)
        if len(message) > 1900:
            message = message[:1870] + "\n…一覧が長いため省略しました。"
    await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("環境変数 DISCORD_BOT_TOKEN が設定されていません。")
    bot.run(TOKEN)
