#!/usr/bin/env python3
import json

with open("watchlist.json") as f:
    wl = json.load(f)

to_remove = ["3850bb", "4bb827"]

for hexcode in to_remove:
    if hexcode in wl:
        removed = wl.pop(hexcode)
        print(f"削除: {hexcode} {removed}")
    else:
        print(f"見つかりませんでした: {hexcode}")

with open("watchlist.json", "w", encoding="utf-8") as f:
    json.dump(wl, f, ensure_ascii=False, indent=4)
    f.write("\n")

print("watchlist.json を更新しました。")
