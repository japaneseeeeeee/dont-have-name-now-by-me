#!/usr/bin/env python3
"""
api.adsb.one (無料・登録不要のADS-B公開API)から、
現在ADS-Bで捕捉されているB-1B Lancerをリアルタイムで取得し、
watchlist.json にマージするスクリプト。

注意:
  これは「今まさに飛んでいる機体」しか拾えません。
  OpenSkyの静的データベースと違い、過去の記録は無いので、
  1回の実行では全機は集まりません。
  日を変えて何度か実行すると、少しずつ機体が増えていきます。
  (cronやlaunchdで定期実行するのもおすすめです)

使い方:
  python3 add_b1b_live.py
"""

import json
from pathlib import Path

import requests

WATCHLIST_PATH = Path("watchlist.json")
API_URL = "https://api.adsb.lol/v2/type/B1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fetch_b1b():
    resp = requests.get(API_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("ac", [])


def main():
    if not WATCHLIST_PATH.exists():
        print(f"エラー: {WATCHLIST_PATH} が見つかりません。")
        return

    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        watchlist = json.load(f)

    try:
        aircraft = fetch_b1b()
    except Exception as e:
        print(f"APIの取得に失敗しました: {e}")
        return

    if not aircraft:
        print("現在ADS-Bで捕捉されているB-1Bはいませんでした。")
        print("日を変えて再実行してみてください。")
        return

    added = 0
    skipped = 0

    for ac in aircraft:
        hexcode = (ac.get("hex") or "").strip().lower()
        if not hexcode:
            continue

        if hexcode in watchlist:
            skipped += 1
            continue

        reg = (ac.get("r") or "").strip()
        typecode = (ac.get("t") or "B1").strip()
        label = reg if reg else hexcode.upper()

        watchlist[hexcode] = {"label": label, "type": f"B-1B Lancer ({typecode})"}
        added += 1
        print(f"追加: {hexcode} {label}")

    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=4)
        f.write("\n")

    print(f"追加: {added}件 / 既存のためスキップ: {skipped}件")
    print(f"{WATCHLIST_PATH} を更新しました。")


if __name__ == "__main__":
    main()
