import os
import sys
import json
import gzip
import urllib.request
import urllib.error

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
        registration = str(args[0]).strip().upper()
        found = lookup_tar1090(registration)

        if not found:
            discord_send(
                channel_id,
                f"❓ `{registration}` の機体情報をデータベースから取得できませんでした。",
            )
            return

        icao24, type_name, canonical_reg = found

        discord_send(
            channel_id,
            f"🔎 `{canonical_reg}` の機体情報を確認しました。\n"
            f"icao24: `{icao24}`\n"
            f"機種: {type_name}\n"
            f"現在位置はADS-Bで確認できませんでした。",
        )
        return

    raise RuntimeError(f"unknown operation: {op}")


if __name__ == "__main__":
    main()
