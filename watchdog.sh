#!/bin/bash
#
# aircraft-alert watchdog
#
#  1. monitor.py が異常に長時間(MAX_RUNTIME秒以上)動き続けていたら、ハングとみなして強制終了する。
#  2. Bot が生きているか確認する。bot_heartbeat(Botが30秒おきに更新)が古ければ、
#     「プロセスは動いているのにDiscordに接続できていない」状態とみなして再起動する。
#
# launchd から5分おきに実行される想定。

MAX_RUNTIME=${MAX_RUNTIME:-300}       # monitor.py がこれ以上動き続けていたらハング扱い(秒)

BOT_LABEL="com.itouosamukou.aircraft-alert-bot"
BOT_MAX_STALE=${BOT_MAX_STALE:-180}     # heartbeat がこれ以上古ければ異常(秒)
BOT_MIN_UPTIME=${BOT_MIN_UPTIME:-120}    # 起動直後のBotは判定しない(秒)
SLEEP_GAP=${SLEEP_GAP:-660}         # 前回実行からこれ以上空いていたらスリープ明けとみなし、今回のBot判定は見送る(秒)

DIR="${AIRCRAFT_DIR:-$HOME/aircraft-alert}"
LOG="$DIR/watchdog.log"
HEARTBEAT="$DIR/bot_heartbeat"
STAMP="$DIR/.watchdog_last_run"
ENV_FILE="$DIR/.env"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}

# ファイルの更新時刻(epoch秒)。macOS(BSD stat)とLinux(GNU stat)の両対応
file_mtime() {
    if [ "$(uname)" = "Darwin" ]; then
        stat -f %m "$1"
    else
        stat -c %Y "$1"
    fi
}

# .env の Webhook に1行送る(失敗しても無視)
notify_discord() {
    [ -f "$ENV_FILE" ] || return 0
    local url
    url=$(grep -E '^AIRCRAFT_WEBHOOK_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    url=${url//\"/}
    url=${url//\'/}
    [ -n "$url" ] || return 0
    curl -s -m 10 -H "Content-Type: application/json" \
        -d "{\"content\": \"$1\"}" "$url" > /dev/null 2>&1
}

# ---------- 1. monitor.py のハング検知 ----------

PID=$(pgrep -f "aircraft-alert/monitor.py")

if [ -n "$PID" ]; then
    ETIME=$(ps -p "$PID" -o etimes= | tr -d ' ')

    if [ -n "$ETIME" ] && [ "$ETIME" -gt "$MAX_RUNTIME" ]; then
        log "[WARN] monitor.py (PID $PID) が ${ETIME}秒 経過しておりハングと判断。強制終了します。"
        kill -9 "$PID"
    fi
fi

# ---------- 2. Bot の生存確認 ----------

now=$(date +%s)
last=0
[ -f "$STAMP" ] && last=$(cat "$STAMP" 2>/dev/null)
[[ "$last" =~ ^[0-9]+$ ]] || last=0
echo "$now" > "$STAMP"

# Macがスリープしていた直後は、Botもまだ再接続中のはず。次回(5分後)に判定する
gap=$((now - last))
if [ "$gap" -gt "$SLEEP_GAP" ]; then
    log "[INFO] 前回実行から ${gap}秒 空いたため(スリープ明け?)Botの判定を見送ります。"
    exit 0
fi

BOT_PID=$(launchctl list 2>/dev/null | awk -v l="$BOT_LABEL" '$3==l {print $1}')

if [ -z "$BOT_PID" ]; then
    log "[WARN] Botのジョブ($BOT_LABEL)がlaunchdに登録されていません。"
    exit 0
fi

reason=""
if [ "$BOT_PID" = "-" ]; then
    reason="Botのプロセスが停止していたため"
else
    uptime=$(ps -p "$BOT_PID" -o etimes= | tr -d ' ')
    if [ -n "$uptime" ] && [ "$uptime" -lt "$BOT_MIN_UPTIME" ]; then
        exit 0    # 起動直後
    fi
    [ -f "$HEARTBEAT" ] || exit 0    # 旧版のBotなど、heartbeatをまだ書かないもの
    age=$((now - $(file_mtime "$HEARTBEAT")))
    if [ "$age" -gt "$BOT_MAX_STALE" ]; then
        reason="Botが${age}秒間応答していなかったため"
    fi
fi

if [ -n "$reason" ]; then
    log "[WARN] ${reason}、Botを再起動します。"
    launchctl kickstart -k "gui/$(id -u)/$BOT_LABEL" >> "$LOG" 2>&1
    notify_discord "🔄 ${reason}、Botを再起動しました。"
fi
