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
from urllib.parse import unquote
from datetime import datetime, timedelta, timezone

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks

from route_corrections import correct_route
from livery import lookup_livery
from equipment_alerts import add_rule, aircraft_matches, empty_store, normalize_equipment
from destination_alerts import (MAX_RULES as DESTINATION_ALERT_LIMIT, add_rule as add_destination_rule, empty_store as empty_destination_store, event_fingerprint, prune_notified as prune_destination_notified, route_matches_destination, should_notify as should_notify_destination)


def load_env_file(path):
    """権限を絞った.envから、未設定の環境変数だけを読み込む。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


ENV_PATH = os.path.expanduser(os.environ.get("AIRCRAFT_ENV_FILE", "~/aircraft-alert/.env"))
load_env_file(ENV_PATH)

WATCHLIST_PATH = os.path.expanduser("~/aircraft-alert/watchlist.json")
WATCHLIST_LOCK_PATH = WATCHLIST_PATH + ".lock"
SHARED_WATCHLIST_URL = os.environ.get(
    "SHARED_WATCHLIST_URL",
    "https://api.github.com/repos/japaneseeeeeee/dont-have-name-now-by-me/contents/watchlist.json?ref=main",
)
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
SYSTEM_ALERT_EVENTS_PATH = os.path.join(BASE_DIR, "system_alert_events.json")
PERSONAL_SPECIAL_NOTIFIED_PATH = os.path.join(BASE_DIR, "personal_special_notified.json")
PERSONAL_ALERT_ACTIONS_PATH = os.path.join(BASE_DIR, "personal_alert_actions.json")
EQUIPMENT_ALERTS_PATH = os.path.join(BASE_DIR, "equipment_alerts.json")
DESTINATION_ALERTS_PATH = os.path.join(BASE_DIR, "destination_alerts.json")
HEARTBEAT_INTERVAL = 30                                        # 秒
CATCHUP_MAX_AGE = timedelta(hours=12)                          # これより古い取りこぼしは処理しない
CATCHUP_LIMIT = 50                                             # 1回の再接続で拾う最大件数
FEEDBACK_SOURCE_CHANNEL_ID = int(os.environ.get("FEEDBACK_SOURCE_CHANNEL_ID", "1554454760038465576"))
FEEDBACK_DESTINATION_CHANNEL_ID = int(
    os.environ.get("FEEDBACK_DESTINATION_CHANNEL_ID", "1552647426253389926")
)
FEEDBACK_OWNER_ID = int(os.environ.get("FEEDBACK_OWNER_ID", "1083347827041771561"))
FEEDBACK_MARKER = "📮"
PHOTO_CHANNEL_ID = int(os.environ.get("PHOTO_CHANNEL_ID", "1552676837581389956"))
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")
PERSONAL_SPECIAL_LIMIT = 20
PERSONAL_DESTINATION_LIMIT = 20
PERSONAL_AIRPORT_LIMIT = 10
PERSONAL_AIRPORTS = ("HND", "NRT", "CTS", "ITM", "KIX", "NGO", "FUK", "OKA")
PERSONAL_LAUNCHER_CHANNEL_NAME = "🔒｜個人設定を作る"
PERSONAL_LAUNCHER_TOPIC = "aircraft-personal-launcher"
PERSONAL_CATEGORY_NAMES = ("🔒｜個人設定", "🔒｜個人設定2", "🔒｜個人設定3")
PERSONAL_CHANNEL_LIMIT = 150
DISCORD_CATEGORY_CHANNEL_LIMIT = 50
PERSONAL_LIMIT_NOTICE_COOLDOWN = 60 * 60
EQUIPMENT_ALERT_LIMIT = 20
EQUIPMENT_ALERT_CHANNEL_ID = int(
    os.environ.get("EQUIPMENT_ALERT_CHANNEL_ID", "1553395098249732157")
)
DESTINATION_ALERT_CHANNEL_ID = int(os.environ.get("DESTINATION_ALERT_CHANNEL_ID", str(EQUIPMENT_ALERT_CHANNEL_ID)))

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
    "my-watch-add": "!my-watch-add <登録記号 または icao24>",
    "my-watch-remove": "!my-watch-remove <登録記号 または icao24>",
    "my-watch-list": "!my-watch-list",
    "my-watch-regions": "!my-watch-regions <地方名... または all>",
    "my-watch-quiet": "!my-watch-quiet <開始時刻> <終了時刻> または off",
    "my-watch-filter": "!my-watch-filter <status|airline|type|show|reset> [条件...]",
    "my-watch-panel": "!my-watch-panel",
    "my-airport-add": "!my-airport-add <空港コード> [半径km]",
    "my-airport-remove": "!my-airport-remove <空港コード>",
    "my-airport-list": "!my-airport-list",
    "equipment-add": "!equipment-add <便名> <機材コード>",
    "equipment-remove": "!equipment-remove <登録ID>",
    "equipment-list": "!equipment-list",
    "equipment-add-server": "!equipment-add-server <便名> <機材コード>",
    "equipment-remove-server": "!equipment-remove-server <登録ID>",
    "equipment-list-server": "!equipment-list-server",
    "destination-add": "!destination-add <登録記号またはicao24> <目的空港>",
    "destination-remove": "!destination-remove <登録ID>",
    "destination-list": "!destination-list",
    "my-destination-add": "!my-destination-add <登録記号またはicao24> <目的空港>",
    "my-destination-remove": "!my-destination-remove <登録ID>",
    "my-destination-list": "!my-destination-list",
    "my-destination-panel": "!my-destination-panel",
    "personal-panel-setup": "!personal-panel-setup",
    "personal-panel-create": "!personal-panel-create",
    "feedback-panel-setup": "!feedback-panel-setup",
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
        self.add_view(PersonalPanelLauncherView())
        self.add_view(PersonalControlPanelView())
        self.add_view(FeedbackPanelView())
        self.add_view(PhotoChannelPanelView())
        self.add_view(EquipmentChannelPanelView())


bot = AircraftBot(command_prefix=PREFIX, intents=intents)


# ============ watchlist の読み書き ============

def load_watchlist():
    """GitHub版を優先し、成功時はローカルの障害時用コピーも更新する。"""
    try:
        response = requests.get(
            SHARED_WATCHLIST_URL,
            headers={"Accept": "application/vnd.github.raw+json", "User-Agent": "aircraft-alert-bot/1.0"},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("watchlist must be an object")
        save_watchlist(data)
        return data
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        logger.warning("共通watchlistを取得できないためローカルコピーを使用: %s", exc)

    with open(WATCHLIST_LOCK_PATH, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        try:
            with open(WATCHLIST_PATH, "r", encoding="utf-8") as stream:
                return json.load(stream)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def save_watchlist(data):
    tmp_path = WATCHLIST_PATH + ".tmp"
    with open(WATCHLIST_LOCK_PATH, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            with open(tmp_path, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
                tmp.write("\n")
            os.replace(tmp_path, WATCHLIST_PATH)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def load_shared_watchlist_for_quality_check():
    """データ品質確認では、監視処理と同じGitHub上の共通watchlistを正とする。"""
    try:
        response = requests.get(
            SHARED_WATCHLIST_URL,
            headers={"Accept": "application/vnd.github.raw+json", "User-Agent": "aircraft-alert-bot/1.0"},
            timeout=8,
        )
        response.raise_for_status()
        watchlist = response.json()
        if not isinstance(watchlist, dict):
            raise ValueError("watchlist must be an object")
        return watchlist
    except (requests.RequestException, ValueError) as exc:
        # ローカルコピーが古い場合に誤った「機種不明」を送らないよう、
        # 共通watchlistを確認できない回は品質通知だけを見送る。
        logger.warning("データ品質確認用watchlistを取得できません: %s", exc)
        return None


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


def consume_system_alert_events():
    """監視処理が作った管理者向け障害イベントをまとめて取り出す。"""
    path, lock_path, tmp_path = SYSTEM_ALERT_EVENTS_PATH, SYSTEM_ALERT_EVENTS_PATH + ".lock", SYSTEM_ALERT_EVENTS_PATH + ".tmp"
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


def append_system_alert_events(events):
    """管理者DMの一時的な失敗分をキューへ戻す。"""
    if not events:
        return
    path, lock_path, tmp_path = SYSTEM_ALERT_EVENTS_PATH, SYSTEM_ALERT_EVENTS_PATH + ".lock", SYSTEM_ALERT_EVENTS_PATH + ".tmp"
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
            with open(tmp_path, "w", encoding="utf-8") as data_file:
                json.dump((queued if isinstance(queued, list) else []) + events, data_file, ensure_ascii=False, indent=2)
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
            "https://hexdb.io/reg-hex",
            params={"reg": str(registration).strip().upper()},
            timeout=5,
        )
        text = r.text.strip()
        if r.status_code == 200 and HEX6.fullmatch(text):
            return text.lower()
    except requests.RequestException as e:
        logger.warning(f"reg-hex lookup failed: {e}")
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
    api_responded = False
    for base in ADSB_API_BASES:
        try:
            r = requests.get(
                f"{base}/v2/{kind}/{value}", timeout=ADSB_TIMEOUT, headers=HTTP_HEADERS
            )
            if r.status_code == 404:
                api_responded = True
                continue
            if r.status_code != 200:
                continue
            api_responded = True
            aircraft = r.json().get("ac") or []
            if aircraft:
                return aircraft
        except (requests.RequestException, ValueError):
            continue
    if not api_responded:
        raise AdsbUnavailableError("all ADS-B providers are unavailable")
    return []


class AdsbUnavailableError(RuntimeError):
    """すべてのADS-B提供元に接続できなかった場合。"""


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

    livery_name = lookup_livery(reg)
    if livery_name:
        embed.add_field(name="🎨 塗装名", value=livery_name, inline=False)

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


class PersonalAlertView(discord.ui.View):
    """個人DM通知だけに付ける操作ボタン。"""

    def __init__(self, user_id, icao24, airport=None):
        super().__init__(timeout=7 * 24 * 3600)
        self.user_id = int(user_id)
        self.icao24 = str(icao24).lower()
        self.airport = str(airport or "").upper()
        # DiscordのInteraction EndpointがCloudflareを向いているため、
        # Worker側でも判別できる固定custom_idを必ず付ける。
        self.stop_aircraft.custom_id = (
            f"personal_alert|stop|{self.user_id}|{self.icao24}|{self.airport}"
        )
        self.mute_one_hour.custom_id = f"personal_alert|mute|{self.user_id}|1h"
        self.mute_six_hours.custom_id = f"personal_alert|mute|{self.user_id}|6h"
        self.mute_until_morning.custom_id = f"personal_alert|mute|{self.user_id}|morning"
        self.mute_day.custom_id = f"personal_alert|mute|{self.user_id}|24h"
        if self.airport:
            self.stop_aircraft.label = f"{self.airport}の空港通知を停止"
        self.add_item(discord.ui.Button(
            label="地図を見る", emoji="🗺️",
            url=f"https://globe.adsbexchange.com/?icao={self.icao24}",
            row=2,
        ))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("この通知は登録者本人だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(
        label="この機体の通知を停止", emoji="🔕",
        style=discord.ButtonStyle.danger, custom_id="personal_alert|stop",
    )
    async def stop_aircraft(self, interaction, button):
        settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
        config = settings.get(str(self.user_id)) or {}
        registered = config.get("aircraft") or {}
        if self.airport:
            airports = config.get("airports") or {}
            removed = airports.pop(self.airport, None)
            config["airports"] = airports
        else:
            removed = registered.pop(self.icao24, None)
        config["aircraft"] = registered
        settings[str(self.user_id)] = config
        save_locked_json(PERSONAL_SPECIALS_PATH, settings)
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            (f"🔕 {self.airport}の空港通知を停止しました。" if self.airport
             else "🔕 この機体の個人通知を停止しました。")
            if removed else "対象はすでに解除されています。",
            ephemeral=True,
        )

    async def _mute(self, interaction, duration):
        settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
        config = settings.get(str(self.user_id)) or {"aircraft": {}}
        deadline, label = personal_mute_deadline(duration)
        config["muted_until"] = deadline
        settings[str(self.user_id)] = config
        save_locked_json(PERSONAL_SPECIALS_PATH, settings)
        await interaction.response.send_message(f"🌙 個人通知を{label}休止しました。", ephemeral=True)

    @discord.ui.button(
        label="1時間休止", emoji="🌙", row=1,
        style=discord.ButtonStyle.secondary, custom_id="personal_alert|mute|1h",
    )
    async def mute_one_hour(self, interaction, button):
        await self._mute(interaction, "1h")

    @discord.ui.button(
        label="6時間休止", emoji="🌙", row=1,
        style=discord.ButtonStyle.secondary, custom_id="personal_alert|mute|6h",
    )
    async def mute_six_hours(self, interaction, button):
        await self._mute(interaction, "6h")

    @discord.ui.button(
        label="翌朝7時まで休止", emoji="🌅", row=1,
        style=discord.ButtonStyle.secondary, custom_id="personal_alert|mute|morning",
    )
    async def mute_until_morning(self, interaction, button):
        await self._mute(interaction, "morning")

    @discord.ui.button(
        label="24時間休止", emoji="🌙", row=1,
        style=discord.ButtonStyle.secondary, custom_id="personal_alert|mute|24h",
    )
    async def mute_day(self, interaction, button):
        await self._mute(interaction, "24h")


def personal_mute_deadline(duration, clicked_at=None):
    """休止種別から終了時刻と利用者向け表示名を返す。翌朝は日本時間の翌日7時。"""
    clicked_at = time.time() if clicked_at is None else float(clicked_at)
    duration = str(duration or "24h").lower()
    if duration == "1h":
        return clicked_at + 3600, "1時間"
    if duration == "6h":
        return clicked_at + 6 * 3600, "6時間"
    if duration == "morning":
        jst = timezone(timedelta(hours=9))
        clicked_jst = datetime.fromtimestamp(clicked_at, tz=jst)
        next_morning = (clicked_jst + timedelta(days=1)).replace(
            hour=7, minute=0, second=0, microsecond=0
        )
        return next_morning.timestamp(), "翌朝7時まで"
    return clicked_at + 24 * 3600, "24時間"


def apply_personal_alert_action(user_id, operation, icao24="", airport="", action_id=""):
    """通知DMの操作を反映し、利用者向けの結果文を返す。"""
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(str(user_id)) or {"aircraft": {}}
    if operation == "alert_mute":
        # Discord Interaction IDから押下時刻を復元すると、再送されても休止期限が
        # 延長されない。旧形式は従来どおり処理時刻を使う。
        try:
            clicked_at = ((int(action_id) >> 22) + 1420070400000) / 1000
        except (TypeError, ValueError):
            clicked_at = time.time()
        config["muted_until"], label = personal_mute_deadline(icao24, clicked_at)
        result = f"🌙 個人通知を{label}休止しました。"
    elif operation == "alert_stop":
        airport = str(airport or "").upper()
        if airport:
            airports = config.get("airports") or {}
            removed = airports.pop(airport, None)
            config["airports"] = airports
            result = (f"🔕 {airport}の空港通知を停止しました。"
                      if removed else "対象はすでに解除されています。")
        else:
            registered = config.get("aircraft") or {}
            removed = registered.pop(str(icao24 or "").lower(), None)
            config["aircraft"] = registered
            result = ("🔕 この機体の個人通知を停止しました。"
                      if removed else "対象はすでに解除されています。")
    else:
        return "⚠️ この通知操作には対応していません。"
    settings[str(user_id)] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    return result


class AirportWatchModal(discord.ui.Modal, title="空港ウォッチを追加"):
    airport = discord.ui.TextInput(label="空港コード", placeholder="HND / NRT / CTS", max_length=3)
    radius = discord.ui.TextInput(label="通知半径（km）", placeholder="50", required=False, max_length=3)

    def __init__(self, user_id):
        super().__init__()
        self.user_id = int(user_id)

    async def on_submit(self, interaction):
        code = str(self.airport).strip().upper()
        if code not in PERSONAL_AIRPORTS:
            await interaction.response.send_message("対応空港: " + " / ".join(PERSONAL_AIRPORTS), ephemeral=True)
            return
        try:
            radius = max(10, min(int(str(self.radius) or "50"), 200))
        except ValueError:
            await interaction.response.send_message("半径は10〜200kmの数字で入力してください。", ephemeral=True)
            return
        settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
        config = settings.get(str(self.user_id)) or {"enabled": True, "aircraft": {}}
        airports = config.get("airports") or {}
        airports[code] = {"radius_km": radius}
        config["airports"] = airports
        settings[str(self.user_id)] = config
        save_locked_json(PERSONAL_SPECIALS_PATH, settings)
        await interaction.response.send_message(f"✅ {code}を半径{radius}kmで空港ウォッチへ追加しました。", ephemeral=True)


class PersonalSettingsView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=15 * 60)
        self.user_id = int(user_id)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("本人だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="登録・条件を表示", emoji="📋", style=discord.ButtonStyle.primary, custom_id="personal_action|summary")
    async def show_settings(self, interaction, button):
        config = load_locked_json(PERSONAL_SPECIALS_PATH, {}).get(str(self.user_id)) or {}
        aircraft = config.get("aircraft") or {}
        filters = config.get("filters") or {}
        airports = config.get("airports") or {}
        lines = [f"DM通知: {'ON' if config.get('enabled', True) else 'OFF'}", f"登録機: {len(aircraft)}機"]
        lines.append("空港: " + (", ".join(f"{code}({value.get('radius_km', 50)}km)" for code, value in airports.items()) or "未登録"))
        lines.append("状態: " + filters.get("status", "all"))
        lines.append("航空会社: " + (", ".join(filters.get("airlines") or []) or "すべて"))
        lines.append("機種: " + (", ".join(filters.get("types") or []) or "すべて"))
        await interaction.response.send_message("👤 **個人設定**\n" + "\n".join(lines), ephemeral=True)

    @discord.ui.button(label="通知ON/OFF", emoji="🔔", style=discord.ButtonStyle.secondary, custom_id="personal_action|settings_toggle")
    async def toggle(self, interaction, button):
        settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
        config = settings.get(str(self.user_id)) or {"aircraft": {}}
        config["enabled"] = not config.get("enabled", True)
        settings[str(self.user_id)] = config
        save_locked_json(PERSONAL_SPECIALS_PATH, settings)
        await interaction.response.send_message(f"DM通知を **{'ON' if config['enabled'] else 'OFF'}** にしました。", ephemeral=True)

    @discord.ui.button(label="空港を追加", emoji="🏢", style=discord.ButtonStyle.success, custom_id="personal_action|airport_add")
    async def add_airport(self, interaction, button):
        await interaction.response.send_modal(AirportWatchModal(self.user_id))

    @discord.ui.button(label="絞り込み解除", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="personal_action|filters_reset")
    async def reset_filters(self, interaction, button):
        settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
        config = settings.get(str(self.user_id)) or {"aircraft": {}}
        config.pop("filters", None)
        settings[str(self.user_id)] = config
        save_locked_json(PERSONAL_SPECIALS_PATH, settings)
        await interaction.response.send_message("飛行状態・航空会社・機種の絞り込みを解除しました。", ephemeral=True)


async def create_personal_destination_rule(user_id, aircraft, destination):
    resolved = await asyncio.to_thread(resolve_personal_aircraft, aircraft)
    if resolved is None:
        return f"⚠️ `{aircraft}` のICAO24を特定できませんでした。"
    icao24, registration, aircraft_type = resolved
    store = load_locked_json(DESTINATION_ALERTS_PATH, empty_destination_store())
    personal_rules = [
        rule for rule in store.get("rules", [])
        if rule.get("scope") == "personal" and str(rule.get("owner_id")) == str(user_id)
    ]
    if len(personal_rules) >= PERSONAL_DESTINATION_LIMIT:
        return f"⚠️ 個人早期通知は{PERSONAL_DESTINATION_LIMIT}件まで登録できます。"
    rule, created = add_destination_rule(
        store, registration=registration, icao24=icao24, aircraft_type=aircraft_type,
        destination=destination, owner_id=user_id, scope="personal",
    )
    if rule is None:
        return "⚠️ 空港コードは `NRT` または `RJAA` のように3〜4文字で指定してください。"
    if not created:
        return f"ℹ️ ID `{rule['id']}`：`{rule['registration']}` → `{rule['destination']}` は登録済みです。"
    save_locked_json(DESTINATION_ALERTS_PATH, store)
    return f"✅ ID `{rule['id']}`：`{rule['registration']}` → `{rule['destination']}` を個人早期通知へ登録しました。"


def personal_destination_rules(user_id):
    store = load_locked_json(DESTINATION_ALERTS_PATH, empty_destination_store())
    return store, [
        rule for rule in store.get("rules", [])
        if rule.get("scope") == "personal" and str(rule.get("owner_id")) == str(user_id)
    ]


class PersonalDestinationAddModal(discord.ui.Modal, title="個人早期通知を追加"):
    aircraft = discord.ui.TextInput(label="登録記号またはICAO24", placeholder="A7-BBA")
    destination = discord.ui.TextInput(label="目的空港", placeholder="NRT / RJAA", min_length=3, max_length=4)

    def __init__(self, user_id):
        super().__init__()
        self.user_id = int(user_id)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await create_personal_destination_rule(
            self.user_id, str(self.aircraft), str(self.destination)
        )
        await interaction.followup.send(message, ephemeral=True)


class PersonalDestinationRemoveModal(discord.ui.Modal, title="個人早期通知を解除"):
    rule_id = discord.ui.TextInput(label="登録ID", placeholder="1", max_length=8)

    def __init__(self, user_id):
        super().__init__()
        self.user_id = int(user_id)

    async def on_submit(self, interaction):
        try:
            target_id = int(str(self.rule_id))
        except ValueError:
            await interaction.response.send_message("登録IDは数字で入力してください。", ephemeral=True)
            return
        store, rules = personal_destination_rules(self.user_id)
        rule = next((item for item in rules if item.get("id") == target_id), None)
        if rule is None:
            await interaction.response.send_message(f"⚠️ ID `{target_id}` は見つかりませんでした。", ephemeral=True)
            return
        store["rules"].remove(rule)
        save_locked_json(DESTINATION_ALERTS_PATH, store)
        await interaction.response.send_message(
            f"🗑️ `{rule['registration']}` → `{rule['destination']}` を解除しました。", ephemeral=True
        )


class PersonalDestinationView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=15 * 60)
        self.user_id = int(user_id)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("本人だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="追加", emoji="➕", style=discord.ButtonStyle.success, custom_id="personal_action|destination_add")
    async def add_rule_button(self, interaction, button):
        await interaction.response.send_modal(PersonalDestinationAddModal(self.user_id))

    @discord.ui.button(label="一覧", emoji="📋", style=discord.ButtonStyle.primary, custom_id="personal_action|destination_list")
    async def list_rules_button(self, interaction, button):
        _, rules = personal_destination_rules(self.user_id)
        if not rules:
            await interaction.response.send_message("個人早期通知は登録されていません。", ephemeral=True)
            return
        lines = [f"ID `{rule['id']}`｜`{rule['registration']}` → `{rule['destination']}`" for rule in rules]
        await interaction.response.send_message("🌍 **個人早期通知**\n" + "\n".join(lines), ephemeral=True)

    @discord.ui.button(label="解除", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="personal_action|destination_remove")
    async def remove_rule_button(self, interaction, button):
        await interaction.response.send_modal(PersonalDestinationRemoveModal(self.user_id))


class PersonalAircraftAddModal(discord.ui.Modal, title="個人機体通知を追加"):
    aircraft = discord.ui.TextInput(label="登録記号またはICAO24", placeholder="JA784A")
    level = discord.ui.TextInput(label="通知レベル", placeholder="NORMAL または SPECIAL", default="NORMAL")

    async def on_submit(self, interaction):
        priority = str(self.level).strip().upper()
        if priority not in {"NORMAL", "SPECIAL"}:
            await interaction.response.send_message("通知レベルは NORMAL または SPECIAL で入力してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await asyncio.to_thread(resolve_personal_aircraft, str(self.aircraft))
        if result is None:
            await interaction.followup.send("機体を確認できませんでした。", ephemeral=True)
            return
        icao24, label, type_name = result
        settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
        config = settings.get(str(interaction.user.id)) or {"enabled": True, "aircraft": {}}
        registered = config.get("aircraft") or {}
        if icao24 not in registered and len(registered) >= PERSONAL_SPECIAL_LIMIT:
            await interaction.followup.send(f"個人watchlistは{PERSONAL_SPECIAL_LIMIT}機までです。", ephemeral=True)
            return
        registered[icao24] = {"label": label, "type": type_name, "priority": priority}
        config["aircraft"] = registered
        settings[str(interaction.user.id)] = config
        save_locked_json(PERSONAL_SPECIALS_PATH, settings)
        await interaction.followup.send(f"✅ `{label}`を **{priority}** で登録しました。", ephemeral=True)


class PersonalEquipmentAddModal(discord.ui.Modal, title="個人機材通知を追加"):
    flight = discord.ui.TextInput(label="便名", placeholder="JL12")
    equipment = discord.ui.TextInput(label="機材コード", placeholder="B77W")

    async def on_submit(self, interaction):
        store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
        current = equipment_rules_for(store, interaction.user.id, "personal")
        if len(current) >= EQUIPMENT_ALERT_LIMIT:
            await interaction.response.send_message(f"機材通知は{EQUIPMENT_ALERT_LIMIT}件までです。", ephemeral=True)
            return
        rule, created = add_rule(store, owner_id=interaction.user.id, scope="personal", flight=str(self.flight), equipment=str(self.equipment))
        if not rule["flight"] or not rule["equipment"]:
            await interaction.response.send_message("便名と機材コードを確認してください。", ephemeral=True)
            return
        if created:
            save_locked_json(EQUIPMENT_ALERTS_PATH, store)
        message = (f"✅ ID `{rule['id']}`：`{rule['flight']}` × `{rule['equipment']}` を登録しました。" if created else f"ℹ️ ID `{rule['id']}` は登録済みです。")
        await interaction.response.send_message(message, ephemeral=True)


def personal_panel_summary(user_id):
    config = load_locked_json(PERSONAL_SPECIALS_PATH, {}).get(str(user_id)) or {}
    _, destinations = personal_destination_rules(user_id)
    equipment = equipment_rules_for(load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store()), user_id, "personal")
    return (
        f"DM通知: **{'ON' if config.get('enabled', True) else 'OFF'}**\n"
        f"✈️ 登録機体: **{len(config.get('aircraft') or {})}機**\n"
        f"🌍 目的地早期通知: **{len(destinations)}件**\n"
        f"🏢 空港ウォッチ: **{len(config.get('airports') or {})}件**\n"
        f"🔔 機材投入通知: **{len(equipment)}件**"
    )


def delete_personal_notification_data(user_id):
    """個人チャンネル削除時に、その利用者の通知設定と待機データをすべて消す。"""
    owner_id = str(user_id)
    removed = {"settings": 0, "destinations": 0, "equipment": 0, "events": 0, "history": 0}

    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    if settings.pop(owner_id, None) is not None:
        removed["settings"] = 1
        save_locked_json(PERSONAL_SPECIALS_PATH, settings)

    events = load_locked_json(PERSONAL_SPECIAL_EVENTS_PATH, [])
    if isinstance(events, list):
        kept_events = [event for event in events if str(event.get("user_id")) != owner_id]
        removed["events"] = len(events) - len(kept_events)
        if removed["events"]:
            save_locked_json(PERSONAL_SPECIAL_EVENTS_PATH, kept_events)

    notified = load_locked_json(PERSONAL_SPECIAL_NOTIFIED_PATH, {})
    if isinstance(notified, dict):
        history_keys = [key for key in notified if str(key).startswith(f"{owner_id}:")]
        for key in history_keys:
            notified.pop(key, None)
        removed["history"] += len(history_keys)
        if history_keys:
            save_locked_json(PERSONAL_SPECIAL_NOTIFIED_PATH, notified)

    destination_store = load_locked_json(DESTINATION_ALERTS_PATH, empty_destination_store())
    destination_rules = destination_store.get("rules") or []
    kept_destinations = [
        rule for rule in destination_rules
        if not (rule.get("scope") == "personal" and str(rule.get("owner_id")) == owner_id)
    ]
    removed["destinations"] = len(destination_rules) - len(kept_destinations)
    if removed["destinations"]:
        destination_store["rules"] = kept_destinations
        save_locked_json(DESTINATION_ALERTS_PATH, destination_store)

    equipment_store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
    equipment_rules = equipment_store.get("rules") or []
    removed_rule_ids = {
        str(rule.get("id")) for rule in equipment_rules
        if rule.get("scope") == "personal" and str(rule.get("owner_id")) == owner_id
    }
    kept_equipment = [rule for rule in equipment_rules if str(rule.get("id")) not in removed_rule_ids]
    removed["equipment"] = len(equipment_rules) - len(kept_equipment)
    if removed_rule_ids:
        equipment_store["rules"] = kept_equipment
        equipment_notified = equipment_store.get("notified") or {}
        equipment_store["notified"] = {
            key: value for key, value in equipment_notified.items()
            if str(key).split(":", 1)[0] not in removed_rule_ids
        }
        save_locked_json(EQUIPMENT_ALERTS_PATH, equipment_store)

    return removed


PERSONAL_PANEL_BRIDGE_PREFIX = "__PERSONAL_PANEL__|"
PERSONAL_ALERT_BRIDGE_PREFIX = "__PERSONAL_ALERT__|"
PERSONAL_ALERT_ACTION_HISTORY_LIMIT = 500
_personal_limit_notified_at = {}


def parse_personal_alert_bridge(content):
    """Discord上に残した通知操作キューを解析する。旧形式も移行期間中は受け付ける。"""
    text = str(content or "")
    marker_at = text.find(PERSONAL_ALERT_BRIDGE_PREFIX)
    if marker_at >= 0:
        payload = text[marker_at:].split("||", 1)[0].strip()
        parts = payload.split("|")
        if len(parts) >= 4 and parts[1].isdigit():
            return {
                "user_id": int(parts[1]),
                "operation": parts[2],
                "action_id": parts[3],
                "args": parts[4:],
            }
        return None

    if text.startswith(PERSONAL_PANEL_BRIDGE_PREFIX):
        parts = text.split("|")
        if len(parts) >= 3 and parts[1].isdigit() and parts[2] in {"alert_stop", "alert_mute"}:
            return {
                "user_id": int(parts[1]),
                "operation": parts[2],
                "action_id": "",
                "args": parts[3:],
            }
    return None


def personal_alert_action_handled(action_id):
    if not action_id:
        return False
    state = load_locked_json(PERSONAL_ALERT_ACTIONS_PATH, {"handled": []})
    return str(action_id) in set(str(value) for value in state.get("handled", []))


def mark_personal_alert_action_handled(action_id):
    if not action_id:
        return
    state = load_locked_json(PERSONAL_ALERT_ACTIONS_PATH, {"handled": []})
    handled = [str(value) for value in state.get("handled", []) if value]
    action_id = str(action_id)
    if action_id not in handled:
        handled.append(action_id)
    state["handled"] = handled[-PERSONAL_ALERT_ACTION_HISTORY_LIMIT:]
    save_locked_json(PERSONAL_ALERT_ACTIONS_PATH, state)


async def handle_personal_alert_bridge(message):
    """Workerの通知操作を実行する。失敗時はメッセージを残し、再接続後に再試行する。"""
    queued = parse_personal_alert_bridge(message.content)
    if queued is None:
        return False
    user_id = queued["user_id"]
    operation = queued["operation"]
    action_id = queued["action_id"]
    args = queued["args"]
    recipient = getattr(message.channel, "recipient", None)
    if message.guild is not None or (recipient is not None and recipient.id != user_id):
        logger.warning("個人通知DM操作を拒否: user=%s channel=%s", user_id, message.channel.id)
        return True
    if operation not in {"alert_stop", "alert_mute"}:
        logger.warning("未対応の個人通知DM操作: %s", operation)
        return True

    if action_id and personal_alert_action_handled(action_id):
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return True

    try:
        result = apply_personal_alert_action(
            user_id,
            operation,
            args[0] if args else "",
            args[1] if len(args) > 1 else "",
            action_id=action_id,
        )
        mark_personal_alert_action_handled(action_id)
        await message.channel.send(result)
        await message.delete()
        logger.info("個人通知DM操作を反映: operation=%s user=%s", operation, user_id)
    except Exception as exc:
        # キュー本体を消さないことで、Mac/Bot復旧後のcatch-upで再試行できる。
        logger.exception("個人通知DM操作の反映に失敗: %s", exc)
        try:
            await message.channel.send(
                "⚠️ 通知設定の変更を保留しています。Bot復旧後に自動で再試行します。"
            )
        except discord.HTTPException:
            pass
    return True


personal_alert_catchup_lock = asyncio.Lock()


async def catch_up_personal_alert_bridges():
    """Mac停止中にDiscord DMへ残った通知操作を再接続時に処理する。"""
    async with personal_alert_catchup_lock:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
        user_ids = {int(value) for value in settings if str(value).isdigit()}
        channels = {
            channel.id: channel for channel in list(bot.private_channels)
            if isinstance(channel, discord.DMChannel)
        }
        # BotのREADYで過去DMがキャッシュされない場合もあるため、登録利用者のDMを
        # 明示的に開く。これでMac停止中の操作も取りこぼさない。
        for user_id in sorted(user_ids):
            try:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                channel = user.dm_channel or await user.create_dm()
                channels[channel.id] = channel
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("個人通知DMの確認準備に失敗: user=%s error=%s", user_id, exc)

        for channel in channels.values():
            try:
                async for message in channel.history(limit=50, oldest_first=True):
                    if not bot.user or message.author.id != bot.user.id:
                        continue
                    if message.created_at < cutoff:
                        continue
                    if parse_personal_alert_bridge(message.content) is not None:
                        await handle_personal_alert_bridge(message)
            except discord.HTTPException as exc:
                logger.warning("個人通知操作のcatch-up失敗: channel=%s error=%s", channel.id, exc)


async def notify_personal_channel_limit(guild, member, current_count):
    """個人チャンネル上限への到達を管理者へDMする（同一利用者は1時間に1回）。"""
    if not bot.is_ready() or bot.is_closed():
        return
    now = time.time()
    notice_key = (getattr(guild, "id", None), getattr(member, "id", None))
    if now - _personal_limit_notified_at.get(notice_key, 0) < PERSONAL_LIMIT_NOTICE_COOLDOWN:
        return
    try:
        owner = await bot.fetch_user(FEEDBACK_OWNER_ID)
        embed = discord.Embed(
            title="⚠️ 個人チャンネル上限に到達",
            description=(
                f"**{getattr(member, 'display_name', member)}** "
                f"(`{getattr(member, 'id', '不明')}`) が個人チャンネルの作成を試みました。"
            ),
            color=0xF39C12,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="サーバー", value=f"{getattr(guild, 'name', '不明')} (`{getattr(guild, 'id', '不明')}`)",
            inline=False,
        )
        embed.add_field(
            name="現在の個人チャンネル数",
            value=f"{current_count} / {PERSONAL_CHANNEL_LIMIT}",
            inline=False,
        )
        await owner.send(embed=embed)
        _personal_limit_notified_at[notice_key] = now
    except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as exc:
        logger.error("個人チャンネル上限の管理者通知に失敗しました: %s", exc)


async def create_personal_settings_channel(guild, member):
    """本人専用チャンネルを重複させず、3カテゴリへ最大150件作成する。"""
    marker = f"aircraft-personal-panel:{member.id}"
    existing = next((channel for channel in guild.text_channels if marker in str(channel.topic or "")), None)
    if existing:
        return existing, False

    personal_channels = [
        channel for channel in guild.text_channels
        if "aircraft-personal-panel:" in str(channel.topic or "")
    ]
    if len(personal_channels) >= PERSONAL_CHANNEL_LIMIT:
        await notify_personal_channel_limit(guild, member, len(personal_channels))
        return None, False

    category = None
    for category_name in PERSONAL_CATEGORY_NAMES:
        candidate = discord.utils.get(guild.categories, name=category_name)
        if candidate is not None and len(candidate.channels) < DISCORD_CATEGORY_CHANNEL_LIMIT:
            category = candidate
            break
    if category is None:
        for category_name in PERSONAL_CATEGORY_NAMES:
            if discord.utils.get(guild.categories, name=category_name) is None:
                category = await guild.create_category(
                    category_name, reason="航空機Bot 個人設定パネル"
                )
                break
    if category is None:
        await notify_personal_channel_limit(guild, member, len(personal_channels))
        return None, False
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True, read_message_history=True),
    }
    safe_name = re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龠_-]", "-", member.display_name.lower()).strip("-")[:40] or str(member.id)
    channel = await guild.create_text_channel(
        f"個人設定-{safe_name}", category=category, overwrites=overwrites,
        topic=marker, reason="航空機Bot 個人設定パネル",
    )
    embed = discord.Embed(
        title="✈️ 航空機Bot 個人設定",
        description="下のボタンから個人通知を設定できます。登録内容は本人とBot以外には表示されません。",
        color=0x5865F2,
    )
    embed.set_footer(text="個人設定パネル")
    panel = await channel.send(embed=embed, view=PersonalControlPanelView())
    try:
        await panel.pin(reason="個人設定パネルを常に表示するため")
    except discord.HTTPException:
        pass
    return channel, True


async def handle_personal_panel_bridge(message):
    """Workerが受けた個人パネル操作を、ローカル保存データへ安全に反映する。"""
    content = str(message.content or "")
    if not content.startswith(PERSONAL_PANEL_BRIDGE_PREFIX):
        return False
    parts = content.split("|")
    if len(parts) < 3 or not parts[1].isdigit():
        return True
    user_id = int(parts[1])
    operation = parts[2]
    args = parts[3:]
    topic = str(getattr(message.channel, "topic", "") or "")
    if operation == "launcher_refresh":
        if PERSONAL_LAUNCHER_TOPIC not in topic:
            logger.warning("個人設定入口パネルの移動を拒否: user=%s channel=%s", user_id, message.channel.id)
            return True
        try:
            async for old_message in message.channel.history(limit=100):
                if old_message.id == message.id:
                    continue
                if (old_message.author.id == bot.user.id and old_message.embeds
                        and str(old_message.embeds[0].footer.text or "") == "personal-launcher-panel"):
                    try:
                        await old_message.delete()
                    except discord.HTTPException:
                        pass
            await message.channel.send("✅ 個人設定作成パネルを一番下へ移動しました。", delete_after=5)
            await send_personal_launcher_panel(message.channel)
        except discord.HTTPException as exc:
            logger.error("個人設定入口パネルの移動に失敗: %s", exc)
            await message.channel.send("⚠️ パネルの移動に失敗しました。", delete_after=15)
        finally:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        return True
    if operation == "create":
        if PERSONAL_LAUNCHER_TOPIC not in topic or message.guild is None:
            logger.warning("個人チャンネル作成を拒否: user=%s channel=%s", user_id, message.channel.id)
            return True
        try:
            member = message.guild.get_member(user_id) or await message.guild.fetch_member(user_id)
            channel, created = await create_personal_settings_channel(message.guild, member)
            if channel is None:
                text = f"<@{user_id}> ⚠️ 個人チャンネルは最大{PERSONAL_CHANNEL_LIMIT}人までです。"
            else:
                text = (f"<@{user_id}> ✅ 専用チャンネルを作成しました：{channel.mention}"
                        if created else f"<@{user_id}> 専用チャンネルはすでにあります：{channel.mention}")
            await message.channel.send(text, delete_after=30, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.Forbidden:
            await message.channel.send("チャンネルを作成できません。Botに「チャンネルの管理」権限を付けてください。", delete_after=30)
        except discord.HTTPException as exc:
            logger.error("個人設定チャンネルの作成に失敗: %s", exc)
            await message.channel.send("チャンネル作成に失敗しました。しばらくしてから再度お試しください。", delete_after=30)
        finally:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        return True
    if f"aircraft-personal-panel:{user_id}" not in topic:
        logger.warning("個人パネル連携を拒否: user=%s channel=%s", user_id, message.channel.id)
        return True

    result = "⚠️ この操作には対応していません。"
    try:
        if operation == "aircraft_add" and args:
            aircraft = args[0]
            priority = (args[1] if len(args) > 1 else "NORMAL").upper()
            if priority not in {"NORMAL", "SPECIAL"}:
                result = "⚠️ 通知レベルは NORMAL または SPECIAL で指定してください。"
            else:
                resolved = await asyncio.to_thread(resolve_personal_aircraft, aircraft)
                if resolved is None:
                    result = f"⚠️ `{aircraft}` の機体を確認できませんでした。"
                else:
                    icao24, label, type_name = resolved
                    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
                    config = settings.get(str(user_id)) or {"enabled": True, "aircraft": {}}
                    registered = config.get("aircraft") or {}
                    if icao24 not in registered and len(registered) >= PERSONAL_SPECIAL_LIMIT:
                        result = f"⚠️ 個人watchlistは{PERSONAL_SPECIAL_LIMIT}機までです。"
                    else:
                        registered[icao24] = {"label": label, "type": type_name, "priority": priority}
                        config["aircraft"] = registered
                        settings[str(user_id)] = config
                        save_locked_json(PERSONAL_SPECIALS_PATH, settings)
                        result = f"✅ `{label}` ({icao24} / {type_name}) を **{priority}** で登録しました。"
        elif operation == "aircraft_list":
            config = load_locked_json(PERSONAL_SPECIALS_PATH, {}).get(str(user_id)) or {}
            registered = config.get("aircraft") or {}
            result = ("✈️ **個人機体通知**\n" + "\n".join(
                f"`{value.get('label') or icao24}` ({icao24})｜{str(value.get('priority') or 'NORMAL').upper()}"
                for icao24, value in sorted(registered.items())
            )) if registered else "個人機体通知は登録されていません。"
        elif operation == "aircraft_remove" and args:
            target = args[0].strip().upper()
            settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
            config = settings.get(str(user_id)) or {}
            registered = config.get("aircraft") or {}
            found = next((icao24 for icao24, value in registered.items()
                          if icao24.upper() == target or str(value.get("label") or "").upper() == target), None)
            if found is None:
                result = f"⚠️ `{target}` は個人機体通知に見つかりませんでした。"
            else:
                label = registered[found].get("label") or found
                del registered[found]
                config["aircraft"] = registered
                settings[str(user_id)] = config
                save_locked_json(PERSONAL_SPECIALS_PATH, settings)
                result = f"🗑️ `{label}` ({found}) の個人機体通知を解除しました。"
        elif operation == "destination_add" and len(args) >= 2:
            result = await create_personal_destination_rule(user_id, args[0], args[1])
        elif operation == "destination_list":
            _, rules = personal_destination_rules(user_id)
            result = ("🌍 **個人早期通知**\n" + "\n".join(
                f"ID `{rule['id']}`｜`{rule['registration']}` → `{rule['destination']}`" for rule in rules
            )) if rules else "個人早期通知は登録されていません。"
        elif operation == "destination_remove" and args and args[0].isdigit():
            target_id = int(args[0])
            store, rules = personal_destination_rules(user_id)
            rule = next((item for item in rules if item.get("id") == target_id), None)
            if rule is None:
                result = f"⚠️ ID `{target_id}` は見つかりませんでした。"
            else:
                store["rules"].remove(rule)
                save_locked_json(DESTINATION_ALERTS_PATH, store)
                result = f"🗑️ `{rule['registration']}` → `{rule['destination']}` を解除しました。"
        elif operation == "airport_add" and args:
            code = args[0].upper()
            try:
                radius = max(10, min(int(args[1] if len(args) > 1 and args[1] else "50"), 200))
            except ValueError:
                radius = 0
            if code not in PERSONAL_AIRPORTS:
                result = "⚠️ 対応空港: " + " / ".join(PERSONAL_AIRPORTS)
            elif not radius:
                result = "⚠️ 半径は10〜200kmの数字で入力してください。"
            else:
                settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
                config = settings.get(str(user_id)) or {"enabled": True, "aircraft": {}}
                airports = config.get("airports") or {}
                if code not in airports and len(airports) >= PERSONAL_AIRPORT_LIMIT:
                    result = f"⚠️ 空港ウォッチは{PERSONAL_AIRPORT_LIMIT}空港までです。"
                else:
                    airports[code] = {"radius_km": radius}
                    config["airports"] = airports
                    settings[str(user_id)] = config
                    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
                    result = f"✅ `{code}`を半径{radius}kmで空港ウォッチへ追加しました。"
        elif operation == "airport_list":
            config = load_locked_json(PERSONAL_SPECIALS_PATH, {}).get(str(user_id)) or {}
            airports = config.get("airports") or {}
            result = ("🏢 **空港ウォッチ**\n" + "\n".join(
                f"`{code}`｜半径{value.get('radius_km', 50)}km" for code, value in sorted(airports.items())
            )) if airports else "空港ウォッチは登録されていません。"
        elif operation == "airport_remove" and args:
            code = args[0].strip().upper()
            settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
            config = settings.get(str(user_id)) or {}
            airports = config.get("airports") or {}
            if airports.pop(code, None) is None:
                result = f"⚠️ `{code}` は空港ウォッチに見つかりませんでした。"
            else:
                config["airports"] = airports
                settings[str(user_id)] = config
                save_locked_json(PERSONAL_SPECIALS_PATH, settings)
                result = f"🗑️ `{code}` の空港ウォッチを解除しました。"
        elif operation == "equipment_add" and len(args) >= 2:
            store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
            current = equipment_rules_for(store, user_id, "personal")
            if len(current) >= EQUIPMENT_ALERT_LIMIT:
                result = f"⚠️ 機材通知は{EQUIPMENT_ALERT_LIMIT}件までです。"
            else:
                rule, created = add_rule(store, owner_id=user_id, scope="personal", flight=args[0], equipment=args[1])
                if not rule["flight"] or not rule["equipment"]:
                    result = "⚠️ 便名と機材コードを確認してください。"
                elif created:
                    save_locked_json(EQUIPMENT_ALERTS_PATH, store)
                    result = f"✅ ID `{rule['id']}`：`{rule['flight']}` × `{rule['equipment']}` を登録しました。"
                else:
                    result = f"ℹ️ ID `{rule['id']}` は登録済みです。"
        elif operation == "equipment_list":
            store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
            rules = equipment_rules_for(store, user_id, "personal")
            result = ("🔔 **個人機材投入通知**\n" + "\n".join(
                f"ID `{rule['id']}`｜`{rule['flight']}` × `{rule['equipment']}`" for rule in rules
            )) if rules else "個人機材投入通知は登録されていません。"
        elif operation == "equipment_remove" and args and args[0].isdigit():
            target_id = int(args[0])
            store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
            rules = equipment_rules_for(store, user_id, "personal")
            rule = next((item for item in rules if item.get("id") == target_id), None)
            if rule is None:
                result = f"⚠️ ID `{target_id}` は見つかりませんでした。"
            else:
                store["rules"].remove(rule)
                save_locked_json(EQUIPMENT_ALERTS_PATH, store)
                result = f"🗑️ `{rule['flight']}` × `{rule['equipment']}` を解除しました。"
        elif operation == "settings_toggle":
            settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
            config = settings.get(str(user_id)) or {"aircraft": {}}
            config["enabled"] = not config.get("enabled", True)
            settings[str(user_id)] = config
            save_locked_json(PERSONAL_SPECIALS_PATH, settings)
            result = f"✅ 個人通知を **{'ON' if config['enabled'] else 'OFF'}** にしました。"
        elif operation == "filters_reset":
            settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
            config = settings.get(str(user_id)) or {"aircraft": {}}
            config.pop("filters", None)
            settings[str(user_id)] = config
            save_locked_json(PERSONAL_SPECIALS_PATH, settings)
            result = "✅ 飛行状態・航空会社・機種の絞り込みを解除しました。"
        elif operation == "summary":
            result = "👤 **現在の個人設定**\n" + personal_panel_summary(user_id)
        elif operation == "refresh":
            async for old_message in message.channel.history(limit=100):
                if old_message.id == message.id:
                    continue
                if (old_message.author.id == bot.user.id and old_message.embeds
                        and old_message.embeds[0].title == "✈️ 航空機Bot 個人設定"):
                    try:
                        await old_message.delete()
                    except discord.HTTPException:
                        pass
            await message.channel.send("✅ 個人設定パネルを一番下へ移動しました。", delete_after=5)
            embed = discord.Embed(
                title="✈️ 航空機Bot 個人設定",
                description="下のボタンから個人通知を設定できます。登録内容は本人とBot以外には表示されません。",
                color=0x5865F2,
            )
            embed.set_footer(text="個人設定パネル")
            await message.channel.send(embed=embed, view=PersonalControlPanelView())
            result = ""
        elif operation == "channel_delete":
            channel = message.channel
            await channel.delete(reason=f"本人の操作による個人設定チャンネル削除: {user_id}")
            removed = await asyncio.to_thread(delete_personal_notification_data, user_id)
            try:
                recipient = message.guild.get_member(user_id) if message.guild else None
                if recipient is None:
                    recipient = await bot.fetch_user(user_id)
                await recipient.send(
                    "🗑️ 個人設定チャンネルを削除しました。個人通知の登録内容と設定データもすべて削除したため、個人通知は停止しました。"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            logger.info("個人設定とチャンネルを削除: user=%s removed=%s", user_id, removed)
            return True
        if result:
            await message.channel.send(result[:1990])
    except Exception as exc:
        logger.exception("個人パネル連携の処理に失敗: %s", exc)
        await message.channel.send("⚠️ 個人設定の処理に失敗しました。しばらくしてから再度お試しください。")
    finally:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    return True


class PersonalControlPanelView(discord.ui.View):
    """本人専用チャンネルへ固定する、再起動後も動く統合パネル。"""
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction):
        topic = str(getattr(interaction.channel, "topic", "") or "")
        marker = f"aircraft-personal-panel:{interaction.user.id}"
        if marker in topic:
            return True
        await interaction.response.send_message("このパネルはチャンネルの所有者本人だけが操作できます。", ephemeral=True)
        return False

    # Discordの上限（5行×5個）に収まるため、各機能を1行ずつ配置する。
    # Interaction EndpointはWorkerのため、ここのcallbackは永続View登録用。
    @discord.ui.button(label="機体を通知登録", emoji="✈️", style=discord.ButtonStyle.success, custom_id="personal_action|aircraft_add", row=0)
    async def aircraft_add(self, interaction, button): pass
    @discord.ui.button(label="機体の登録一覧", emoji="📋", style=discord.ButtonStyle.primary, custom_id="personal_action|aircraft_list", row=0)
    async def aircraft_list(self, interaction, button): pass
    @discord.ui.button(label="機体の登録解除", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="personal_action|aircraft_remove", row=0)
    async def aircraft_remove(self, interaction, button): pass

    @discord.ui.button(label="目的地を通知登録", emoji="🌍", style=discord.ButtonStyle.success, custom_id="personal_action|destination_add", row=1)
    async def destination_add(self, interaction, button): pass
    @discord.ui.button(label="目的地の登録一覧", emoji="📋", style=discord.ButtonStyle.primary, custom_id="personal_action|destination_list", row=1)
    async def destination_list(self, interaction, button): pass
    @discord.ui.button(label="目的地の登録解除", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="personal_action|destination_remove", row=1)
    async def destination_remove(self, interaction, button): pass

    @discord.ui.button(label="空港周辺を通知登録", emoji="🏢", style=discord.ButtonStyle.success, custom_id="personal_action|airport_add", row=2)
    async def airport_add(self, interaction, button): pass
    @discord.ui.button(label="空港の登録一覧", emoji="📋", style=discord.ButtonStyle.primary, custom_id="personal_action|airport_list", row=2)
    async def airport_list(self, interaction, button): pass
    @discord.ui.button(label="空港の登録解除", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="personal_action|airport_remove", row=2)
    async def airport_remove(self, interaction, button): pass

    @discord.ui.button(label="便・機材を通知登録", emoji="🔔", style=discord.ButtonStyle.success, custom_id="personal_action|equipment_add", row=3)
    async def equipment_add(self, interaction, button): pass
    @discord.ui.button(label="便・機材の登録一覧", emoji="📋", style=discord.ButtonStyle.primary, custom_id="personal_action|equipment_list", row=3)
    async def equipment_list(self, interaction, button): pass
    @discord.ui.button(label="便・機材の登録解除", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="personal_action|equipment_remove", row=3)
    async def equipment_remove(self, interaction, button): pass

    @discord.ui.button(label="通知ON・条件設定", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="personal_panel:settings", row=4)
    async def settings(self, interaction, button):
        await interaction.response.send_message("個人通知の設定です。", view=PersonalSettingsView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="全登録・設定を見る", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="personal_panel:summary", row=4)
    async def summary(self, interaction, button):
        await interaction.response.send_message("👤 **現在の個人設定**\n" + personal_panel_summary(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="パネルを一番下へ", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="personal_panel:refresh", row=4)
    async def refresh(self, interaction, button): pass

    @discord.ui.button(label="個人設定を全削除", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="personal_panel:delete", row=4)
    async def delete_channel(self, interaction, button): pass

    @discord.ui.button(label="この画面の使い方", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="personal_panel:help", row=4)
    async def help(self, interaction, button): pass


async def refresh_personal_control_panels():
    """既存の個人設定パネルを最新ボタンに更新し、必要なら最下部へ移動する。"""
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if "aircraft-personal-panel:" not in str(channel.topic or ""):
                continue
            try:
                async for message in channel.history(limit=50):
                    if (message.author.id == bot.user.id and message.embeds
                            and message.embeds[0].title == "✈️ 航空機Bot 個人設定"):
                        logger.info("個人設定パネルを確認: channel=%s footer=%r", channel.id, message.embeds[0].footer.text)
                        footer_text = str(message.embeds[0].footer.text or "")
                        if footer_text == "個人設定パネル":
                            await message.edit(view=PersonalControlPanelView())
                        elif footer_text == "personal-panel-v3":
                            embed = message.embeds[0].copy()
                            embed.set_footer(text="個人設定パネル")
                            await message.edit(embed=embed, view=PersonalControlPanelView())
                        else:
                            embed = message.embeds[0].copy()
                            embed.set_footer(text="個人設定パネル")
                            new_message = await channel.send(embed=embed, view=PersonalControlPanelView())
                            try:
                                await new_message.pin(reason="個人設定パネルを常に表示するため")
                            except discord.HTTPException:
                                pass
                            await message.delete()
                        break
            except discord.HTTPException as exc:
                logger.error("個人設定パネルの更新に失敗: channel=%s %s", channel.id, exc)


class PersonalPanelLauncherView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="自分専用の通知設定を作る", emoji="🔒", style=discord.ButtonStyle.success, custom_id="personal_panel:create")
    async def create_panel(self, interaction, button):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使用してください。", ephemeral=True)
            return
        marker = f"aircraft-personal-panel:{interaction.user.id}"
        existing = next((channel for channel in guild.text_channels if marker in str(channel.topic or "")), None)
        if existing:
            await interaction.response.send_message(f"専用チャンネルはすでにあります：{existing.mention}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel, _ = await create_personal_settings_channel(guild, interaction.user)
            if channel is None:
                await interaction.followup.send(
                    f"⚠️ 個人チャンネルは最大{PERSONAL_CHANNEL_LIMIT}人までです。",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(f"✅ 専用チャンネルを作成しました：{channel.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("チャンネルを作成できません。Botに「チャンネルの管理」権限を付けてください。", ephemeral=True)
        except discord.HTTPException as exc:
            logger.error("個人設定チャンネルの作成に失敗: %s", exc)
            await interaction.followup.send("チャンネル作成に失敗しました。しばらくしてから再度お試しください。", ephemeral=True)

    @discord.ui.button(label="最新位置へ移動", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="personal_panel:launcher_refresh")
    async def refresh(self, interaction, button): pass

    @discord.ui.button(label="ボタンの説明", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="personal_panel:launcher_help")
    async def help(self, interaction, button): pass


class GeneralMenuView(discord.ui.View):
    """一般コマンドチャンネルの常設メニューを最新表示へ更新する。"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="機体を探して登録", emoji="🔍", style=discord.ButtonStyle.primary, custom_id="menu|aircraft-search", row=0)
    async def search(self, interaction, button): pass
    @discord.ui.button(label="機体の詳細を見る", emoji="🛩️", style=discord.ButtonStyle.secondary, custom_id="menu|info", row=0)
    async def info(self, interaction, button): pass
    @discord.ui.button(label="飛行中の便を探す", emoji="🛫", style=discord.ButtonStyle.primary, custom_id="menu|flight", row=0)
    async def flight(self, interaction, button): pass
    @discord.ui.button(label="現在の発着を見る", emoji="🏢", style=discord.ButtonStyle.secondary, custom_id="menu|airport", row=0)
    async def airport(self, interaction, button): pass

    @discord.ui.button(label="サーバー登録機を見る", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="menu|list", row=1)
    async def list_aircraft(self, interaction, button): pass
    @discord.ui.button(label="ボタンの説明", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="menu|help", row=1)
    async def help(self, interaction, button): pass
    @discord.ui.button(label="最新位置へ移動", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="menu|refresh", row=1)
    async def refresh(self, interaction, button): pass
    @discord.ui.button(label="管理者メニュー", emoji="⚙️", style=discord.ButtonStyle.danger, custom_id="menu|admin", row=1)
    async def admin(self, interaction, button): pass


async def refresh_general_menu_panel():
    """既存の一般コマンドメニューを、新しいボタン名と構成へ更新する。"""
    for guild in bot.guilds:
        channels = [channel for channel in guild.text_channels if "一般コマンド" in channel.name]
        for channel in channels:
            try:
                async for message in channel.history(limit=50):
                    if (message.author.id == bot.user.id
                            and "航空機通知Botメニュー" in str(message.content or "")):
                        await message.edit(view=GeneralMenuView())
                        logger.info("一般コマンドパネルを更新: channel=%s message=%s", channel.id, message.id)
                        break
            except discord.HTTPException as exc:
                logger.error("一般コマンドパネルの更新に失敗: channel=%s %s", channel.id, exc)


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
            priority = str(event.get("priority") or "SPECIAL").upper()
            title_prefix = "🚨 個人SPECIAL" if priority == "SPECIAL" else "🔔 個人watchlist"
            color = 0xED4245 if priority == "SPECIAL" else 0x3498DB
            embed = discord.Embed(
                title=f"{title_prefix}｜{event['label']}を検出",
                description=f"{event['region']}の監視範囲内で{state}。",
                color=color,
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
            route = event.get("route") or {}
            if route.get("origin") and route.get("destination"):
                flight = f"{route['flight_iata']} · " if route.get("flight_iata") else ""
                embed.add_field(
                    name="区間(推定)",
                    value=(
                        f"{flight}{format_airport(route['origin'])} → "
                        f"{format_airport(route['destination'])}"
                    ),
                    inline=False,
                )
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
            await recipient.send(
                embed=embed,
                view=PersonalAlertView(user_id, event["icao24"], event.get("airport")),
            )
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


@tasks.loop(seconds=30)
async def system_alert_dispatch():
    """API制限・認証失敗・監視停止などを管理者へDMする。"""
    if not bot.is_ready() or bot.is_closed():
        return
    events = await asyncio.to_thread(consume_system_alert_events)
    retry_events = []
    for event in events:
        try:
            owner = await bot.fetch_user(FEEDBACK_OWNER_ID)
            status = event.get("status_code")
            description = event.get("summary") or "システムで問題が発生しました。"
            if status is not None:
                description += f"\nHTTP状態: `{status}`"
            detail = str(event.get("detail") or "").strip()
            embed = discord.Embed(
                title=f"⚠️ システム警告｜{event.get('service') or '不明'}",
                description=description,
                color=0xED4245,
                timestamp=datetime.fromtimestamp(float(event.get("detected_at", time.time())), timezone.utc),
            )
            if detail:
                embed.add_field(name="詳細", value=detail[:1000], inline=False)
            embed.set_footer(text="同じ内容の通知は6時間抑制されます")
            await owner.send(embed=embed)
        except (discord.HTTPException, KeyError, TypeError, ValueError) as exc:
            logger.error("システム警告の管理者DMに失敗しました: %s", exc)
            retry_count = int(event.get("retry_count", 0)) + 1
            if retry_count <= 3:
                event["retry_count"] = retry_count
                retry_events.append(event)
    if retry_events:
        await asyncio.to_thread(append_system_alert_events, retry_events)


@tasks.loop(seconds=120)
async def equipment_alert_dispatch():
    """指定便へ対象機材が実際に入ったことをADS-Bで確認して通知する。"""
    if not bot.is_ready() or bot.is_closed():
        return
    store = await asyncio.to_thread(load_locked_json, EQUIPMENT_ALERTS_PATH, empty_store())
    rules = store.get("rules") or []
    if not rules:
        return

    now = time.time()
    notified = store.setdefault("notified", {})
    changed = False
    live_cache = {}
    for rule in list(rules):
        flight = rule.get("flight") or ""
        equipment = rule.get("equipment") or ""
        if not flight or not equipment:
            continue
        if flight not in live_cache:
            live_cache[flight] = await asyncio.to_thread(find_live_aircraft, flight)
        for aircraft in live_cache[flight]:
            if not aircraft_matches(aircraft, equipment):
                continue
            callsign = (aircraft.get("flight") or flight).strip().upper()
            hex_id = (aircraft.get("hex") or "unknown").lower()
            operation = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            event_key = f"{rule['id']}:{operation}:{callsign}:{hex_id}"
            if event_key in notified:
                continue
            route = await asyncio.to_thread(fetch_route, aircraft.get("flight"))
            embed = build_flight_embed(aircraft, route)
            embed.title = f"🔔 指定便の機材を実機確認｜{flight}"
            embed.description = (
                f"**{flight}** に指定機材 **{normalize_equipment(equipment)}** が投入されたことを、"
                "現在のADS-B情報で確認しました。"
            )
            try:
                if rule.get("scope") == "personal":
                    recipient = await bot.fetch_user(int(rule["owner_id"]))
                    await recipient.send(embed=embed)
                else:
                    channel = bot.get_channel(EQUIPMENT_ALERT_CHANNEL_ID)
                    if channel is None:
                        channel = await bot.fetch_channel(EQUIPMENT_ALERT_CHANNEL_ID)
                    await channel.send(embed=embed)
                notified[event_key] = now
                changed = True
            except discord.Forbidden:
                logger.warning("機材通知を送信できません: rule=%s", rule.get("id"))
            except (discord.HTTPException, KeyError, TypeError, ValueError) as exc:
                logger.error("機材通知の送信に失敗しました: rule=%s %s", rule.get("id"), exc)

    cutoff = now - 8 * 86400
    old_keys = [key for key, stamp in notified.items() if float(stamp) < cutoff]
    for key in old_keys:
        notified.pop(key, None)
        changed = True
    if changed:
        await asyncio.to_thread(save_locked_json, EQUIPMENT_ALERTS_PATH, store)


def _destination_distance_km(aircraft, route):
    try:
        destination = route["destination"]
        lat1, lon1 = math.radians(float(aircraft["lat"])), math.radians(float(aircraft["lon"]))
        lat2, lon2 = math.radians(float(destination["latitude"])), math.radians(float(destination["longitude"]))
        dlat, dlon = lat2 - lat1, lon2 - lon1
        value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371.0 * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))
    except (KeyError, TypeError, ValueError):
        return None


def build_destination_alert_embed(rule, aircraft, route):
    callsign = str(aircraft.get("flight") or "").strip().upper()
    hex_id = str(aircraft.get("hex") or rule["icao24"]).lower()
    embed = discord.Embed(title=f"✈️ 監視機体が{rule['destination']}へ向かっています", description=f"**{format_airport(route['origin'])} → {format_airport(route['destination'])}**", color=0x5865F2, timestamp=datetime.now(timezone.utc), url=f"https://globe.adsbexchange.com/?icao={hex_id}")
    embed.add_field(name="登録記号", value=f"`{aircraft.get('r') or rule['registration']}`", inline=True)
    embed.add_field(name="機種", value=aircraft.get("desc") or aircraft.get("t") or rule.get("type") or "不明", inline=True)
    embed.add_field(name="便名", value=f"`{route.get('flight_iata') or callsign}`", inline=True)
    lat, lon = aircraft.get("lat"), aircraft.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        embed.add_field(name="現在位置", value=f"{lat:.3f}, {lon:.3f}", inline=True)
    altitude = aircraft.get("alt_baro")
    embed.add_field(name="高度", value=f"{float(altitude):,.0f} ft" if isinstance(altitude, (int, float)) else "取得不可", inline=True)
    distance = _destination_distance_km(aircraft, route)
    embed.add_field(name=f"{rule['destination']}まで", value=f"約{distance:,.0f} km" if distance is not None else "取得不可", inline=True)
    embed.add_field(name="到着予定", value="取得不可", inline=True)
    embed.set_footer(text="目的地は便ルート情報との完全一致で判定｜到着予定は信頼できる情報源がある場合のみ表示")
    return embed


@tasks.loop(seconds=120)
async def destination_alert_dispatch():
    if not bot.is_ready() or bot.is_closed():
        return
    store = await asyncio.to_thread(load_locked_json, DESTINATION_ALERTS_PATH, empty_destination_store())
    rules = store.get("rules") or []
    if not rules:
        return
    rules_by_icao = {}
    for rule in rules:
        rules_by_icao.setdefault(str(rule.get("icao24") or "").lower(), []).append(rule)
    now, notified, changed, route_cache = time.time(), store.setdefault("notified", {}), False, {}
    for icao24, aircraft_rules in rules_by_icao.items():
        if not HEX6.fullmatch(icao24):
            continue
        aircraft_list = await asyncio.to_thread(adsb_lookup, "hex", icao24)
        aircraft = next((item for item in aircraft_list if str(item.get("hex") or "").lower().lstrip("~") == icao24), None)
        if not aircraft or aircraft.get("alt_baro") == "ground":
            continue
        callsign = str(aircraft.get("flight") or "").strip().upper()
        if not callsign:
            continue
        if callsign not in route_cache:
            route_cache[callsign] = await asyncio.to_thread(fetch_route, callsign)
        route = route_cache[callsign]
        if not route:
            continue
        for rule in aircraft_rules:
            if not route_matches_destination(route, rule.get("destination")):
                continue
            fingerprint = event_fingerprint(rule, aircraft, route)
            if not should_notify_destination(notified, fingerprint, now):
                continue
            try:
                if rule.get("scope") == "personal":
                    recipient = await bot.fetch_user(int(rule["owner_id"]))
                    await recipient.send(embed=build_destination_alert_embed(rule, aircraft, route))
                else:
                    channel = bot.get_channel(DESTINATION_ALERT_CHANNEL_ID) or await bot.fetch_channel(DESTINATION_ALERT_CHANNEL_ID)
                    await channel.send(embed=build_destination_alert_embed(rule, aircraft, route))
                notified[fingerprint], changed = now, True
            except discord.Forbidden:
                logger.warning("早期目的地通知を送信できません: rule=%s", rule.get("id"))
            except (discord.HTTPException, KeyError, TypeError, ValueError) as exc:
                logger.error("早期目的地通知に失敗しました: rule=%s %s", rule.get("id"), exc)
    cleaned = prune_destination_notified(notified, now)
    if cleaned != notified:
        store["notified"], changed = cleaned, True
    if changed:
        await asyncio.to_thread(save_locked_json, DESTINATION_ALERTS_PATH, store)


# ============ イベント ============

FEEDBACK_BRIDGE_PREFIX = "__FEEDBACK__|"


async def forward_feedback(author, description, *, kind="質問・改善要望", created_at=None):
    """質問・改善要望を管理者用チャンネルへ転送する共通処理。"""
    destination = bot.get_channel(FEEDBACK_DESTINATION_CHANNEL_ID)
    if destination is None:
        destination = await bot.fetch_channel(FEEDBACK_DESTINATION_CHANNEL_ID)
    allowed = discord.AllowedMentions(
        everyone=False, roles=False,
        users=[discord.Object(id=FEEDBACK_OWNER_ID)], replied_user=False,
    )
    embed = discord.Embed(
        title=f"📮 新しい{kind}", description=description[:4000],
        color=0x5865F2, timestamp=created_at or discord.utils.utcnow(),
    )
    embed.add_field(name="送信者", value=f"{author.mention} (`{author.id}`)", inline=False)
    await destination.send(f"<@{FEEDBACK_OWNER_ID}>", embed=embed, allowed_mentions=allowed)


async def handle_feedback_bridge(message):
    """Workerの質問パネル操作をローカルBotで転送・再配置する。"""
    content = str(message.content or "")
    if not content.startswith(FEEDBACK_BRIDGE_PREFIX):
        return False
    try:
        _, user_text, operation, encoded, source_message_id = (content.split("|", 4) + ["", ""])[:5]
        if message.channel.id != FEEDBACK_SOURCE_CHANNEL_ID or not user_text.isdigit():
            return True
        if operation == "refresh":
            if source_message_id.isdigit():
                try:
                    old = await message.channel.fetch_message(int(source_message_id))
                    await old.delete()
                except discord.HTTPException:
                    pass
            await send_feedback_panel(message.channel)
        elif operation in {"question", "request"}:
            author = message.guild.get_member(int(user_text)) if message.guild else None
            author = author or await bot.fetch_user(int(user_text))
            text = unquote(encoded).strip()
            await forward_feedback(author, text or "（本文なし）", kind="質問" if operation == "question" else "改善要望")
            try:
                await author.send("✅ 質問・改善要望を管理者へ送信しました。回答はこのDMに届きます。")
            except discord.HTTPException:
                pass
    except (discord.HTTPException, ValueError) as exc:
        logger.error("質問パネル連携に失敗しました: %s", exc)
    finally:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    return True


class FeedbackPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="質問を送る", emoji="❓", style=discord.ButtonStyle.primary, custom_id="feedback:question")
    async def question(self, interaction, button):
        pass

    @discord.ui.button(label="改善要望を送る", emoji="💡", style=discord.ButtonStyle.success, custom_id="feedback:request")
    async def request(self, interaction, button):
        pass

    @discord.ui.button(label="ボタンの説明", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="feedback:help")
    async def help(self, interaction, button):
        pass

    @discord.ui.button(label="最新位置へ移動", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="feedback:refresh")
    async def refresh(self, interaction, button):
        pass


class PhotoChannelPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ボタンの説明・投稿方法", emoji="📖", style=discord.ButtonStyle.primary, custom_id="photo:help")
    async def help(self, interaction, button): pass

    @discord.ui.button(label="機体情報を調べる", emoji="🛩️", style=discord.ButtonStyle.secondary, custom_id="menu|info")
    async def info(self, interaction, button): pass

    @discord.ui.button(label="最新位置へ移動", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="photo:refresh")
    async def refresh(self, interaction, button): pass


class EquipmentChannelPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="機材投入通知を追加", emoji="➕", style=discord.ButtonStyle.success, custom_id="equipment_server:add")
    async def add(self, interaction, button): pass

    @discord.ui.button(label="登録便を見る", emoji="📋", style=discord.ButtonStyle.primary, custom_id="equipment_server:list")
    async def list(self, interaction, button): pass

    @discord.ui.button(label="機材投入通知を解除", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="equipment_server:remove")
    async def remove(self, interaction, button): pass

    @discord.ui.button(label="ボタンの説明", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="equipment_server:help")
    async def help(self, interaction, button): pass

    @discord.ui.button(label="最新位置へ移動", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="equipment_server:refresh")
    async def refresh(self, interaction, button): pass


async def send_photo_channel_panel(channel):
    embed = discord.Embed(
        title="📸 航空機写真の投稿",
        description="写真を添付し、本文の最初に登録記号、その下に撮影場所と感想を書いてください。Botが機体情報付きのスレッドを作成します。",
        color=0x3498DB,
    )
    embed.set_footer(text="photo-channel-panel")
    return await channel.send(embed=embed, view=PhotoChannelPanelView())


async def send_equipment_channel_panel(channel):
    embed = discord.Embed(
        title="🔔 サーバー機材投入通知",
        description="指定便に指定機材が実際に投入されたことをADS-Bで確認すると、このチャンネルへ通知します。設定変更は管理者専用です。",
        color=0xF1C40F,
    )
    embed.set_footer(text="equipment-channel-panel")
    return await channel.send(embed=embed, view=EquipmentChannelPanelView())


async def send_personal_launcher_panel(channel):
    embed = discord.Embed(
        title="🔒 個人設定チャンネルを作成",
        description=(
            "下のボタンを押すと、本人とBotだけが見られる専用チャンネルを作成します。\n"
            "作成後は、機体通知・目的地早期通知・空港ウォッチ・機材投入通知をボタンで設定できます。"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="personal-launcher-panel")
    return await channel.send(embed=embed, view=PersonalPanelLauncherView())


async def ensure_personal_launcher_channel():
    """個人設定の公開入口チャンネルと常設ボタンを用意する。"""
    for guild in bot.guilds:
        channel = next((item for item in guild.text_channels if PERSONAL_LAUNCHER_TOPIC in str(item.topic or "")), None)
        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True, read_message_history=True),
            }
            channel = await guild.create_text_channel(
                PERSONAL_LAUNCHER_CHANNEL_NAME, overwrites=overwrites,
                topic=PERSONAL_LAUNCHER_TOPIC, reason="航空機Bot 個人設定の入口",
            )
        async for message in channel.history(limit=30):
            if (message.author.id == bot.user.id and message.embeds
                    and str(message.embeds[0].footer.text or "") == "personal-launcher-panel"):
                await message.edit(view=PersonalPanelLauncherView())
                break
        else:
            panel = await send_personal_launcher_panel(channel)
            try:
                await panel.pin(reason="個人設定の入口を常に表示するため")
            except discord.HTTPException:
                pass


async def ensure_channel_panel(channel_id, footer_text, sender, view):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    async for message in channel.history(limit=50):
        if (message.author.id == bot.user.id and message.embeds
                and str(message.embeds[0].footer.text or "") == footer_text):
            await message.edit(view=view())
            return message
    message = await sender(channel)
    try:
        await message.pin(reason="操作パネルを常に表示するため")
    except discord.HTTPException:
        pass
    return message


CHANNEL_PANEL_BRIDGE_PREFIX = "__CHANNEL_PANEL__|"


async def handle_channel_panel_bridge(message):
    content = str(message.content or "")
    if not content.startswith(CHANNEL_PANEL_BRIDGE_PREFIX):
        return False
    parts = (content.split("|", 6) + ["", "", "", "", "", "", ""])[:7]
    _, user_text, panel, operation, arg1, arg2, source_message_id = parts
    logger.info(
        "チャンネルパネル操作を受信: panel=%s operation=%s user=%s channel=%s",
        panel, operation, user_text, message.channel.id,
    )
    try:
        member = message.guild.get_member(int(user_text)) if message.guild and user_text.isdigit() else None
        if member is None and message.guild and user_text.isdigit():
            try:
                member = await message.guild.fetch_member(int(user_text))
            except discord.HTTPException:
                member = None
        if panel == "photo" and operation == "refresh":
            if member is None or not member.guild_permissions.administrator:
                return True
            if source_message_id.isdigit():
                try:
                    source_message = await message.channel.fetch_message(int(source_message_id))
                    await source_message.delete()
                except discord.HTTPException:
                    pass
            await send_photo_channel_panel(message.channel)
        elif panel == "equipment":
            if member is None or not member.guild_permissions.administrator:
                return True
            owner_id = message.guild.id
            store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
            logger.info("機材通知データを読込: rules=%s", len(store.get("rules", [])))
            if operation == "list":
                rules = equipment_rules_for(store, owner_id, "server")
                text = ("🔔 **サーバー機材通知の登録一覧**\n" + "\n".join(
                    f"ID `{rule['id']}`｜`{rule['flight']}` × `{rule['equipment']}`" for rule in sorted(rules, key=lambda item: item["id"])
                )) if rules else "登録されているサーバー機材通知はありません。"
                logger.info("機材通知一覧を送信開始: rules=%s", len(rules))
                response = await message.channel.send(text[:1990])
                logger.info("機材通知一覧を送信: message=%s rules=%s", response.id, len(rules))
            elif operation == "add":
                current = equipment_rules_for(store, owner_id, "server")
                if len(current) >= EQUIPMENT_ALERT_LIMIT:
                    await message.channel.send(f"⚠️ 機材通知は{EQUIPMENT_ALERT_LIMIT}件まで登録できます。")
                else:
                    rule, created = add_rule(store, owner_id=owner_id, scope="server", flight=unquote(arg1), equipment=unquote(arg2))
                    if created:
                        save_locked_json(EQUIPMENT_ALERTS_PATH, store)
                    await message.channel.send(
                        f"✅ ID `{rule['id']}`：`{rule['flight']}` × `{rule['equipment']}` を登録しました。" if created
                        else f"ℹ️ ID `{rule['id']}` は登録済みです。"
                    )
            elif operation == "remove" and unquote(arg1).isdigit():
                target_id = int(unquote(arg1))
                rule = next((item for item in equipment_rules_for(store, owner_id, "server") if item.get("id") == target_id), None)
                if rule is None:
                    await message.channel.send(f"⚠️ ID `{target_id}` は見つかりませんでした。")
                else:
                    store["rules"].remove(rule)
                    save_locked_json(EQUIPMENT_ALERTS_PATH, store)
                    await message.channel.send(f"🗑️ `{rule['flight']}` × `{rule['equipment']}` を解除しました。")
            elif operation == "refresh":
                if source_message_id.isdigit():
                    try:
                        source_message = await message.channel.fetch_message(int(source_message_id))
                        await source_message.delete()
                    except discord.HTTPException:
                        pass
                await send_equipment_channel_panel(message.channel)
    except (discord.HTTPException, ValueError) as exc:
        logger.error("チャンネルパネル連携に失敗しました: %s", exc)
    finally:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    return True


async def send_feedback_panel(channel):
    embed = discord.Embed(
        title="📮 質問・改善要望",
        description=(
            "Botについての質問や改善してほしい内容を、下のボタンから送信できます。\n"
            "送信内容は管理者へ転送され、回答はDMで届きます。\n\n"
            "従来どおり、メッセージの先頭に `📮` を付ける方法も使用できます。"
        ),
        color=0x5865F2,
    )
    return await channel.send(embed=embed, view=FeedbackPanelView())


async def ensure_feedback_panel():
    """質問チャンネルにパネルが無い場合だけ自動設置する。"""
    channel = bot.get_channel(FEEDBACK_SOURCE_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(FEEDBACK_SOURCE_CHANNEL_ID)
    async for item in channel.history(limit=50):
        if item.author.id == bot.user.id and item.embeds and item.embeds[0].title == "📮 質問・改善要望":
            return item
    return await send_feedback_panel(channel)

async def notify_feedback(message):
    """質問箱への通常投稿を管理者専用チャンネルへ転送し、元投稿を消す。"""
    if message.author.id == FEEDBACK_OWNER_ID:
        return
    attachments = "\n".join(attachment.url for attachment in message.attachments)
    description = message.content.strip()[len(FEEDBACK_MARKER):].strip()
    description = description or "（本文なし・添付ファイルのみ）"
    if attachments:
        description += f"\n\n**添付ファイル**\n{attachments}"
    try:
        await forward_feedback(message.author, description, created_at=message.created_at)
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

def find_watchlist_anomalies(watchlist):
    anomalies = []
    labels = {}
    for icao24, value in watchlist.items():
        entry = value if isinstance(value, dict) else {"label": value}
        label = str(entry.get("label") or "").strip()
        aircraft_type = str(entry.get("type") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{6}", str(icao24)):
            anomalies.append(f"不正なICAO24: `{icao24}`")
        if not label or label == "?":
            anomalies.append(f"登録記号なし: `{icao24}`")
        if not aircraft_type or aircraft_type == "不明":
            anomalies.append(f"機種不明: `{label or icao24}`")
        normalized = _normalize_reg(label)
        if normalized:
            if normalized in labels and labels[normalized] != icao24:
                anomalies.append(f"登録記号重複: `{label}` ({labels[normalized]} / {icao24})")
            labels[normalized] = icao24
    return anomalies


_last_anomaly_fingerprint = None


@tasks.loop(minutes=30)
async def data_quality_check():
    global _last_anomaly_fingerprint
    if not bot.is_ready() or bot.is_closed():
        return
    watchlist = await asyncio.to_thread(load_shared_watchlist_for_quality_check)
    if watchlist is None:
        return
    anomalies = await asyncio.to_thread(find_watchlist_anomalies, watchlist)
    fingerprint = tuple(anomalies)
    if not anomalies or fingerprint == _last_anomaly_fingerprint:
        _last_anomaly_fingerprint = fingerprint
        return
    _last_anomaly_fingerprint = fingerprint
    owner = await bot.fetch_user(FEEDBACK_OWNER_ID)
    preview = "\n".join(f"• {item}" for item in anomalies[:20])
    more = f"\n…ほか{len(anomalies) - 20}件" if len(anomalies) > 20 else ""
    try:
        await owner.send(f"⚠️ **航空機データ異常を検出**\n{preview}{more}")
    except discord.HTTPException as exc:
        logger.warning("データ異常の管理者DMに失敗しました: %s", exc)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    if not heartbeat.is_running():
        heartbeat.start()
    if not personal_special_dispatch.is_running():
        personal_special_dispatch.start()
    if not system_alert_dispatch.is_running():
        system_alert_dispatch.start()
    if not equipment_alert_dispatch.is_running():
        equipment_alert_dispatch.start()
    if not destination_alert_dispatch.is_running():
        destination_alert_dispatch.start()
    if not data_quality_check.is_running():
        data_quality_check.start()
    try:
        await ensure_feedback_panel()
    except discord.HTTPException as exc:
        logger.error("質問・改善要望パネルの自動設置に失敗しました: %s", exc)
    try:
        await ensure_channel_panel(PHOTO_CHANNEL_ID, "photo-channel-panel", send_photo_channel_panel, PhotoChannelPanelView)
        await ensure_channel_panel(EQUIPMENT_ALERT_CHANNEL_ID, "equipment-channel-panel", send_equipment_channel_panel, EquipmentChannelPanelView)
    except discord.HTTPException as exc:
        logger.error("チャンネル操作パネルの自動設置に失敗しました: %s", exc)
    try:
        await ensure_personal_launcher_channel()
    except discord.HTTPException as exc:
        logger.error("個人設定入口チャンネルの自動設置に失敗しました: %s", exc)
    await refresh_personal_control_panels()
    await refresh_general_menu_panel()
    await catch_up_personal_alert_bridges()
    await catch_up_missed_commands()


@bot.event
async def on_resumed():
    await catch_up_personal_alert_bridges()
    await catch_up_missed_commands()


@bot.event
async def on_message(message):
    if message.author.bot:
        if bot.user and message.author.id == bot.user.id:
            if await handle_personal_alert_bridge(message):
                return
            if await handle_feedback_bridge(message):
                return
            if await handle_channel_panel_bridge(message):
                return
            await handle_personal_panel_bridge(message)
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
        try:
            aircraft_list = await asyncio.to_thread(find_live_aircraft, query)
        except AdsbUnavailableError:
            await ctx.send(
                "⚠️ 現在一時的に利用できません。ADS-Bデータサービスで障害または混雑が"
                "発生しています。少し待ってから再度お試しください。"
            )
            return
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
        "`!my-watch-add JA784A` — 通常の個人watchlistへ追加\n"
        "`!my-watch-remove JA784A` — 個人登録を解除\n"
        "`!my-watch-list` — 個人登録と設定を表示\n"
        "`!my-watch-regions 関東 中部` — 通知する地方を指定\n"
        "`!my-watch-regions all` — 全国の地方を対象\n"
        "`!my-watch-quiet 23:00 07:00` — 通常通知を休止（SPECIALは通知）\n"
        "`!my-watch-quiet off` — 時間制限を解除\n\n"
        "`!my-watch-filter status airborne` — 飛行中だけ通知\n"
        "`!my-watch-filter airline ANA JAL` — 航空会社を限定\n"
        "`!my-watch-filter type B77W A359` — 機種を限定\n"
        "`!my-watch-filter show` — 現在の条件を表示\n"
        "`!my-watch-filter reset` — 絞り込みを解除\n\n"
        "`!my-watch-panel` — ボタン式の個人設定画面\n"
        "`!my-airport-add HND 50` — 空港ウォッチを追加\n"
        "`!my-airport-remove HND` — 空港ウォッチを解除\n"
        "`!my-airport-list` — 登録空港を表示\n\n"
        "登録内容はほかのメンバーには表示されません。"
    )


async def add_personal_aircraft(ctx, aircraft, priority):
    if not await require_personal_special_dm(ctx):
        return
    user_id = str(ctx.author.id)
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(user_id) or {"enabled": True, "aircraft": {}}
    registered = config.get("aircraft") or {}
    if len(registered) >= PERSONAL_SPECIAL_LIMIT:
        await ctx.send(f"⚠️ 個人watchlistは1人{PERSONAL_SPECIAL_LIMIT}機まで登録できます。")
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
        await ctx.send(f"ℹ️ `{label}` ({icao24}) はすでに個人watchlistへ登録されています。")
        return
    registered[icao24] = {"label": label, "type": type_name, "priority": priority}
    config["aircraft"] = registered
    config.setdefault("enabled", True)
    settings[user_id] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(
        f"✅ `{label}` ({icao24} / {type_name}) を個人watchlistへ追加しました。\n"
        f"通知レベル: **{priority}**\n"
        "日本国内で検出すると、ここへDMで通知します。"
    )


@bot.command(name="my-special-add")
async def my_special_add(ctx, aircraft: str):
    await add_personal_aircraft(ctx, aircraft, "SPECIAL")


@bot.command(name="my-watch-add")
async def my_watch_add(ctx, aircraft: str):
    await add_personal_aircraft(ctx, aircraft, "NORMAL")


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
        await ctx.send(f"⚠️ `{aircraft}` は自分の個人watchlistに登録されていません。")
        return
    removed = registered.pop(match)
    config["aircraft"] = registered
    settings[user_id] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(f"🗑️ `{removed.get('label', match)}` ({match}) を個人watchlistから解除しました。")


@bot.command(name="my-watch-remove")
async def my_watch_remove(ctx, aircraft: str):
    await my_special_remove.callback(ctx, aircraft)


@bot.command(name="my-special-list")
async def my_special_list(ctx):
    if not await require_personal_special_dm(ctx):
        return
    config = load_locked_json(PERSONAL_SPECIALS_PATH, {}).get(str(ctx.author.id)) or {}
    registered = config.get("aircraft") or {}
    state = "ON" if config.get("enabled", True) else "OFF"
    if not registered:
        await ctx.send(
            f"👤 **自分の個人watchlist**（DM通知: {state}）\n登録機はありません。\n"
            "`!my-watch-add JA784A` または `!my-special-add JA784A` で追加できます。"
        )
        return
    lines = [
        f"`{entry.get('label', icao24)}` ({icao24} / {entry.get('type') or '不明'}) · **{entry.get('priority') or 'SPECIAL'}**"
        for icao24, entry in sorted(registered.items(), key=lambda item: item[1].get("label", ""))
    ]
    filters = config.get("filters") or {}
    status_labels = {"airborne": "飛行中のみ", "ground": "地上のみ", "all": "すべて"}
    conditions = [f"状態: {status_labels.get(filters.get('status', 'all'), 'すべて')}"]
    if filters.get("airlines"):
        conditions.append("航空会社: " + ", ".join(filters["airlines"]))
    if filters.get("types"):
        conditions.append("機種: " + ", ".join(filters["types"]))
    await ctx.send(
        (f"👤 **自分の個人watchlist**（DM通知: {state} / {len(lines)}機）\n"
         + "通知条件: " + "｜".join(conditions) + "\n" + "\n".join(lines))[:1990]
    )


@bot.command(name="my-watch-list")
async def my_watch_list(ctx):
    await my_special_list.callback(ctx)


PERSONAL_REGION_ALIASES = {
    "北海道": "hokkaido", "東北": "tohoku", "関東": "kanto", "中部": "chubu",
    "近畿": "kinki", "関西": "kinki", "中国・四国": "chugoku_shikoku",
    "中国四国": "chugoku_shikoku", "九州": "kyushu", "沖縄": "okinawa",
}


@bot.command(name="my-watch-regions")
async def my_watch_regions(ctx, *regions: str):
    if not await require_personal_special_dm(ctx):
        return
    if not regions:
        await ctx.send("⚠️ 例: `!my-watch-regions 関東 中部` または `!my-watch-regions all`")
        return
    values = [] if len(regions) == 1 and regions[0].lower() == "all" else [
        PERSONAL_REGION_ALIASES.get(region) for region in regions
    ]
    if any(value is None for value in values):
        await ctx.send("⚠️ 地方名は 北海道・東北・関東・中部・近畿・中国・四国・九州・沖縄 から指定してください。")
        return
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(str(ctx.author.id)) or {"enabled": True, "aircraft": {}}
    config["regions"] = list(dict.fromkeys(values))
    settings[str(ctx.author.id)] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    label = "すべての地方" if not values else "・".join(regions)
    await ctx.send(f"✅ 個人通知の対象を **{label}** に設定しました。")


@bot.command(name="my-watch-quiet")
async def my_watch_quiet(ctx, start: str, end: str = None):
    if not await require_personal_special_dm(ctx):
        return
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(str(ctx.author.id)) or {"enabled": True, "aircraft": {}}
    if start.lower() == "off":
        config.pop("quiet_hours", None)
        message = "✅ 通知時間の制限を解除しました。"
    else:
        pattern = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        if end is None or not pattern.fullmatch(start) or not pattern.fullmatch(end):
            await ctx.send("⚠️ 例: `!my-watch-quiet 23:00 07:00` または `!my-watch-quiet off`")
            return
        config["quiet_hours"] = {"start": start, "end": end}
        message = f"✅ **{start}〜{end}** は通常通知を休止します。SPECIALは通知します。"
    settings[str(ctx.author.id)] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(message)


@bot.command(name="my-watch-filter")
async def my_watch_filter(ctx, field: str, *values: str):
    if not await require_personal_special_dm(ctx):
        return
    field = field.lower()
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(str(ctx.author.id)) or {"enabled": True, "aircraft": {}}
    filters = config.get("filters") or {}

    if field == "show":
        status = {"airborne": "飛行中のみ", "ground": "地上のみ", "all": "すべて"}.get(filters.get("status", "all"), "すべて")
        airlines = ", ".join(filters.get("airlines") or []) or "すべて"
        types = ", ".join(filters.get("types") or []) or "すべて"
        await ctx.send(f"🔔 **現在の個人通知条件**\n状態: {status}\n航空会社: {airlines}\n機種: {types}")
        return
    if field == "reset":
        config.pop("filters", None)
        message = "✅ 飛行状態・航空会社・機種の絞り込みをすべて解除しました。"
    elif field == "status":
        aliases = {"all": "all", "すべて": "all", "airborne": "airborne", "flying": "airborne", "飛行中": "airborne", "ground": "ground", "地上": "ground"}
        value = aliases.get(values[0].lower()) if values else None
        if value is None:
            await ctx.send("⚠️ `!my-watch-filter status all` / `airborne` / `ground` のいずれかを指定してください。")
            return
        filters["status"] = value
        config["filters"] = filters
        message = f"✅ 飛行状態の条件を **{values[0]}** に設定しました。"
    elif field in {"airline", "type"}:
        cleaned = list(dict.fromkeys(value.upper() for value in values if value.strip()))
        if len(cleaned) == 1 and cleaned[0] in {"ALL", "OFF", "すべて"}:
            cleaned = []
        if not cleaned and not values:
            await ctx.send(f"⚠️ 例: `!my-watch-filter {field} ANA JAL`。解除は `all` を指定してください。")
            return
        key = "airlines" if field == "airline" else "types"
        filters[key] = cleaned
        config["filters"] = filters
        label = ", ".join(cleaned) or "すべて"
        message = f"✅ {'航空会社' if field == 'airline' else '機種'}の条件を **{label}** に設定しました。"
    else:
        await ctx.send("⚠️ 項目は `status` / `airline` / `type` / `show` / `reset` から選んでください。")
        return

    settings[str(ctx.author.id)] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(message)


@bot.command(name="my-watch-panel")
async def my_watch_panel(ctx):
    if not await require_personal_special_dm(ctx):
        return
    await ctx.send(
        "👤 **個人通知 設定画面**\n登録内容の確認、通知切り替え、空港追加、絞り込み解除ができます。",
        view=PersonalSettingsView(ctx.author.id),
    )


@bot.command(name="my-airport-add")
async def my_airport_add(ctx, airport: str, radius_km: int = 50):
    if not await require_personal_special_dm(ctx):
        return
    code = airport.strip().upper()
    if code not in PERSONAL_AIRPORTS:
        await ctx.send("⚠️ 対応空港: " + " / ".join(PERSONAL_AIRPORTS))
        return
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(str(ctx.author.id)) or {"enabled": True, "aircraft": {}}
    airports = config.get("airports") or {}
    if code not in airports and len(airports) >= PERSONAL_AIRPORT_LIMIT:
        await ctx.send(f"⚠️ 空港ウォッチは{PERSONAL_AIRPORT_LIMIT}空港まで登録できます。")
        return
    radius_km = max(10, min(radius_km, 200))
    airports[code] = {"radius_km": radius_km}
    config["airports"] = airports
    settings[str(ctx.author.id)] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(f"✅ `{code}`を半径{radius_km}kmで空港ウォッチへ追加しました。")


@bot.command(name="my-airport-remove")
async def my_airport_remove(ctx, airport: str):
    if not await require_personal_special_dm(ctx):
        return
    code = airport.strip().upper()
    settings = load_locked_json(PERSONAL_SPECIALS_PATH, {})
    config = settings.get(str(ctx.author.id)) or {"aircraft": {}}
    airports = config.get("airports") or {}
    if airports.pop(code, None) is None:
        await ctx.send(f"⚠️ `{code}`は登録されていません。")
        return
    config["airports"] = airports
    settings[str(ctx.author.id)] = config
    save_locked_json(PERSONAL_SPECIALS_PATH, settings)
    await ctx.send(f"🗑️ `{code}`を空港ウォッチから解除しました。")


@bot.command(name="my-airport-list")
async def my_airport_list(ctx):
    if not await require_personal_special_dm(ctx):
        return
    config = load_locked_json(PERSONAL_SPECIALS_PATH, {}).get(str(ctx.author.id)) or {}
    airports = config.get("airports") or {}
    if not airports:
        await ctx.send("登録中の空港ウォッチはありません。")
        return
    lines = [f"`{code}`｜半径{value.get('radius_km', 50)}km" for code, value in sorted(airports.items())]
    await ctx.send("🏢 **空港ウォッチ**\n" + "\n".join(lines))


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


async def require_equipment_dm(ctx):
    if ctx.guild is None:
        return True
    await ctx.send(
        "🔒 個人用の機材通知は登録内容を非公開にするため、BotへのDMで使用してください。",
        delete_after=30,
    )
    return False


def equipment_rules_for(store, owner_id, scope):
    return [
        rule for rule in store.get("rules", [])
        if str(rule.get("owner_id")) == str(owner_id) and rule.get("scope") == scope
    ]


async def add_equipment_rule(ctx, flight, equipment, scope, owner_id):
    store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
    current = equipment_rules_for(store, owner_id, scope)
    if len(current) >= EQUIPMENT_ALERT_LIMIT:
        await ctx.send(f"⚠️ 機材通知は{EQUIPMENT_ALERT_LIMIT}件まで登録できます。")
        return
    rule, created = add_rule(
        store,
        owner_id=owner_id,
        scope=scope,
        flight=flight,
        equipment=equipment,
    )
    if not rule["flight"] or not rule["equipment"]:
        await ctx.send("⚠️ 便名と機材コードを確認してください。例: `JL12 B77W`")
        return
    if not created:
        await ctx.send(
            f"ℹ️ ID `{rule['id']}`：`{rule['flight']}` × `{rule['equipment']}` は登録済みです。"
        )
        return
    save_locked_json(EQUIPMENT_ALERTS_PATH, store)
    destination = "DM" if scope == "personal" else "機材変更・投入情報チャンネル"
    await ctx.send(
        f"✅ ID `{rule['id']}`：`{rule['flight']}` に `{rule['equipment']}` が実際に投入されたら、"
        f"{destination}へ通知します。\n予定情報ではなく、飛行中のADS-B情報を約2分ごとに確認します。"
    )


async def remove_equipment_rule(ctx, rule_id, scope, owner_id):
    store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
    match = next(
        (
            rule for rule in store.get("rules", [])
            if rule.get("id") == rule_id
            and rule.get("scope") == scope
            and str(rule.get("owner_id")) == str(owner_id)
        ),
        None,
    )
    if match is None:
        await ctx.send(f"⚠️ ID `{rule_id}` の登録は見つかりませんでした。")
        return
    store["rules"].remove(match)
    save_locked_json(EQUIPMENT_ALERTS_PATH, store)
    await ctx.send(
        f"🗑️ ID `{rule_id}`：`{match['flight']}` × `{match['equipment']}` を解除しました。"
    )


async def list_equipment_rules(ctx, scope, owner_id):
    store = load_locked_json(EQUIPMENT_ALERTS_PATH, empty_store())
    rules = equipment_rules_for(store, owner_id, scope)
    if not rules:
        await ctx.send("登録されている機材通知はありません。")
        return
    lines = [
        f"ID `{rule['id']}`｜`{rule['flight']}` × `{rule['equipment']}`"
        for rule in sorted(rules, key=lambda item: item["id"])
    ]
    await ctx.send(("🔔 **機材通知の登録一覧**\n" + "\n".join(lines))[:1990])


@bot.command(name="equipment-add")
async def equipment_add(ctx, flight: str, equipment: str):
    if not await require_equipment_dm(ctx):
        return
    await add_equipment_rule(ctx, flight, equipment, "personal", ctx.author.id)


@bot.command(name="equipment-remove")
async def equipment_remove(ctx, rule_id: int):
    if not await require_equipment_dm(ctx):
        return
    await remove_equipment_rule(ctx, rule_id, "personal", ctx.author.id)


@bot.command(name="equipment-list")
async def equipment_list(ctx):
    if not await require_equipment_dm(ctx):
        return
    await list_equipment_rules(ctx, "personal", ctx.author.id)


@bot.command(name="equipment-add-server")
@commands.has_permissions(administrator=True)
async def equipment_add_server(ctx, flight: str, equipment: str):
    await add_equipment_rule(ctx, flight, equipment, "server", ctx.guild.id)


@bot.command(name="equipment-remove-server")
@commands.has_permissions(administrator=True)
async def equipment_remove_server(ctx, rule_id: int):
    await remove_equipment_rule(ctx, rule_id, "server", ctx.guild.id)


@bot.command(name="equipment-list-server")
@commands.has_permissions(administrator=True)
async def equipment_list_server(ctx):
    await list_equipment_rules(ctx, "server", ctx.guild.id)


@bot.command(name="destination-add")
@commands.has_permissions(administrator=True)
async def destination_add(ctx, aircraft: str, destination: str):
    async with ctx.typing():
        resolved = await asyncio.to_thread(resolve_personal_aircraft, aircraft)
    if resolved is None:
        await ctx.send(f"⚠️ `{aircraft}` のICAO24を特定できませんでした。登録記号または6桁のICAO24を確認してください。")
        return
    icao24, registration, aircraft_type = resolved
    store = load_locked_json(DESTINATION_ALERTS_PATH, empty_destination_store())
    server_rules = [rule for rule in store.get("rules", []) if rule.get("scope", "server") == "server"]
    if len(server_rules) >= DESTINATION_ALERT_LIMIT:
        await ctx.send(f"⚠️ 早期目的地通知は全体で{DESTINATION_ALERT_LIMIT}件まで登録できます。")
        return
    rule, created = add_destination_rule(store, registration=registration, icao24=icao24, aircraft_type=aircraft_type, destination=destination, owner_id=ctx.guild.id, scope="server")
    if rule is None:
        await ctx.send("⚠️ 空港コードは `NRT` または `RJAA` のように3〜4文字で指定してください。")
        return
    if not created:
        await ctx.send(f"ℹ️ ID `{rule['id']}`：`{rule['registration']}` → `{rule['destination']}` は登録済みです。")
        return
    save_locked_json(DESTINATION_ALERTS_PATH, store)
    await ctx.send(f"✅ ID `{rule['id']}`：`{rule['registration']}` → `{rule['destination']}` を早期通知へ登録しました。\n世界のADS-B情報を約2分ごとに確認し、信頼できる便ルートで目的地が一致した場合だけ通知します。")


@bot.command(name="destination-remove")
@commands.has_permissions(administrator=True)
async def destination_remove(ctx, rule_id: int):
    store = load_locked_json(DESTINATION_ALERTS_PATH, empty_destination_store())
    rule = next((item for item in store.get("rules", []) if item.get("id") == rule_id and item.get("scope", "server") == "server"), None)
    if rule is None:
        await ctx.send(f"⚠️ ID `{rule_id}` の早期通知は見つかりませんでした。")
        return
    store["rules"].remove(rule)
    save_locked_json(DESTINATION_ALERTS_PATH, store)
    await ctx.send(f"🗑️ ID `{rule_id}`：`{rule['registration']}` → `{rule['destination']}` を解除しました。")


@bot.command(name="destination-list")
@commands.has_permissions(administrator=True)
async def destination_list(ctx):
    rules = [
        rule for rule in load_locked_json(DESTINATION_ALERTS_PATH, empty_destination_store()).get("rules") or []
        if rule.get("scope", "server") == "server"
    ]
    if not rules:
        await ctx.send("登録中の早期目的地通知はありません。")
        return
    lines = [f"ID `{rule['id']}`｜`{rule['registration']}` (`{rule['icao24']}`) → `{rule['destination']}`" for rule in sorted(rules, key=lambda item: item["id"])]
    await ctx.send(("🌍 **早期目的地通知の登録一覧**\n" + "\n".join(lines))[:1990])


@bot.command(name="my-destination-add")
async def my_destination_add(ctx, aircraft: str, destination: str):
    if not await require_personal_special_dm(ctx):
        return
    async with ctx.typing():
        message = await create_personal_destination_rule(ctx.author.id, aircraft, destination)
    await ctx.send(message)


@bot.command(name="my-destination-remove")
async def my_destination_remove(ctx, rule_id: int):
    if not await require_personal_special_dm(ctx):
        return
    store, rules = personal_destination_rules(ctx.author.id)
    rule = next((item for item in rules if item.get("id") == rule_id), None)
    if rule is None:
        await ctx.send(f"⚠️ ID `{rule_id}` の個人早期通知は見つかりませんでした。")
        return
    store["rules"].remove(rule)
    save_locked_json(DESTINATION_ALERTS_PATH, store)
    await ctx.send(f"🗑️ `{rule['registration']}` → `{rule['destination']}` を解除しました。")


@bot.command(name="my-destination-list")
async def my_destination_list(ctx):
    if not await require_personal_special_dm(ctx):
        return
    _, rules = personal_destination_rules(ctx.author.id)
    if not rules:
        await ctx.send("個人早期通知は登録されていません。")
        return
    lines = [f"ID `{rule['id']}`｜`{rule['registration']}` (`{rule['icao24']}`) → `{rule['destination']}`" for rule in rules]
    await ctx.send(("🌍 **個人早期通知の登録一覧**\n" + "\n".join(lines))[:1990])


@bot.command(name="my-destination-panel")
async def my_destination_panel(ctx):
    if not await require_personal_special_dm(ctx):
        return
    await ctx.send(
        "🌍 **個人早期通知パネル**\n追加・一覧確認・解除をボタンから操作できます。",
        view=PersonalDestinationView(ctx.author.id),
    )


@bot.command(name="personal-panel-setup")
@commands.has_permissions(administrator=True)
async def personal_panel_setup(ctx):
    embed = discord.Embed(
        title="🔒 航空機Bot 個人設定パネル",
        description=(
            "下のボタン、または `!personal-panel-create` で、本人とBotだけが見られる専用チャンネルを作成します。\n"
            "機体通知・目的地早期通知・空港ウォッチ・機材投入通知を個別に設定できます。"
        ),
        color=0x5865F2,
    )
    await ctx.send(embed=embed, view=PersonalPanelLauncherView())


@bot.command(name="feedback-panel-setup")
@commands.has_permissions(administrator=True)
async def feedback_panel_setup(ctx):
    """質問・改善要望チャンネルへ常設ボタンパネルを設置する。"""
    if ctx.channel.id != FEEDBACK_SOURCE_CHANNEL_ID:
        await ctx.send("このコマンドは質問・改善要望チャンネルで使用してください。")
        return
    await send_feedback_panel(ctx.channel)


@bot.command(name="personal-panel-create")
async def personal_panel_create(ctx):
    if ctx.guild is None:
        await ctx.send("このコマンドはサーバー内で使用してください。")
        return
    guild = ctx.guild
    marker = f"aircraft-personal-panel:{ctx.author.id}"
    existing = next((channel for channel in guild.text_channels if marker in str(channel.topic or "")), None)
    if existing:
        await ctx.send(f"専用チャンネルはすでにあります：{existing.mention}", delete_after=30)
        return
    try:
        channel, _ = await create_personal_settings_channel(guild, ctx.author)
        if channel is None:
            await ctx.send(f"⚠️ 個人チャンネルは最大{PERSONAL_CHANNEL_LIMIT}人までです。")
            return
        await ctx.send(f"✅ 専用チャンネルを作成しました：{channel.mention}", delete_after=30)
    except discord.Forbidden:
        await ctx.send("チャンネルを作成できません。Botに「チャンネルの管理」権限を付けてください。")
    except discord.HTTPException as exc:
        logger.error("個人設定チャンネルの作成に失敗: %s", exc)
        await ctx.send("チャンネル作成に失敗しました。しばらくしてから再度お試しください。")


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
