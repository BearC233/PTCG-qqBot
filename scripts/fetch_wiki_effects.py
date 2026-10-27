#!/usr/bin/env python3
"""从神奇宝贝百科抓取缺失的招式/特性效果文本。

只抓 PokéAPI 覆盖不到的部分：
- moves.csv 里 effect_id 为空的招式
- 超极巨招式（PokéAPI 未收录）
- ability_prose 缺失的特性 + 需要精确数值的关键特性

输出 data/kb/wiki_effects.csv，导入脚本会优先使用这里的文本。
支持断点续跑：已写入的名字会自动跳过。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("BOT_ROOT", "/opt/qqbot"))
CSV_DIR = ROOT / "data" / "csv"
KB_DIR = ROOT / "data" / "kb"
OUT_PATH = KB_DIR / "wiki_effects.csv"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
KEY_ABILITIES = {"sharpness", "tough-claws", "adaptability", "liquid-voice"}


def read_csv(name: str) -> list[dict[str, str]]:
    path = CSV_DIR / name
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def zh_name_map(filename: str, id_field: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in read_csv(filename):
        if row.get("local_language_id") == "12":
            out[row[id_field]] = row["name"].strip()
    return out


def build_targets() -> list[tuple[str, str, str]]:
    """返回 (kind, name_zh, name_en) 列表。"""
    targets: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, name_zh: str, name_en: str) -> None:
        name_zh = (name_zh or "").strip()
        name_en = (name_en or "").strip()
        if not name_zh:
            return
        key = (kind, name_zh)
        if key in seen:
            return
        seen.add(key)
        targets.append((kind, name_zh, name_en))

    move_names = zh_name_map("move_names.csv", "move_id")
    missing_moves = [r for r in read_csv("moves.csv") if not r.get("effect_id")]
    for row in missing_moves:
        add("move", move_names.get(row["id"], ""), row["identifier"])

    move_list_path = KB_DIR / "move_list_zh.json"
    if move_list_path.exists():
        for item in json.loads(move_list_path.read_text(encoding="utf-8")):
            if item.get("category") == "超极巨":
                add("move", item.get("name_zh", ""), item.get("name_en", ""))

    prose = {
        r["ability_id"]
        for r in read_csv("ability_prose.csv")
        if r.get("local_language_id") == "9"
    }
    ability_names = zh_name_map("ability_names.csv", "ability_id")
    abilities = read_csv("abilities.csv")
    for row in abilities:
        if row["id"] not in prose or row["identifier"] in KEY_ABILITIES:
            add("ability", ability_names.get(row["id"], ""), row["identifier"])

    return targets


def fetch_page(name: str) -> str:
    variants = [name, name.replace("・", "·"), name.replace("·", "・")]
    last_error: Exception | None = None
    for variant in dict.fromkeys(variants):
        url = "https://wiki.52poke.com/wiki/" + urllib.parse.quote(variant.replace(" ", "_"))
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    return resp.read().decode("utf-8", "ignore")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 404:
                    break
                time.sleep(2 + attempt * 2)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f"{name}: {last_error}")


def strip_html(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>|<style.*?</style>", "", fragment, flags=re.S)
    fragment = re.sub(r"<sup.*?</sup>", "", fragment, flags=re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(fragment)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_effect(page: str, kind: str) -> str:
    wanted = ("招式附加效果", "特性效果", "效果")
    blocks: dict[str, str] = {}
    for match in re.finditer(r"<h2[^>]*>(.*?)</h2>(.*?)(?=<h2|\Z)", page, re.S):
        title = strip_html(match.group(1))
        blocks.setdefault(title, strip_html(match.group(2)))
    for title in wanted:
        for key, value in blocks.items():
            if title in key and value:
                value = re.sub(r"^主[頁页]面：[^。]{0,40}?）\s*", "", value)
                value = re.sub(r"^主[頁页]面：\S+\s*", "", value)
                return value[:400]
    return ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--kind", choices=["move", "ability"], default=None)
    parser.add_argument("--only", choices=["gmax", "key-abilities"], default=None)
    args = parser.parse_args()

    KB_DIR.mkdir(parents=True, exist_ok=True)
    targets = build_targets()
    if args.kind:
        targets = [t for t in targets if t[0] == args.kind]
    if args.only == "gmax":
        gmax = {
            item.get("name_zh", "")
            for item in json.loads((KB_DIR / "move_list_zh.json").read_text(encoding="utf-8"))
            if item.get("category") == "超极巨"
        }
        targets = [t for t in targets if t[0] == "move" and t[1] in gmax]
    elif args.only == "key-abilities":
        targets = [t for t in targets if t[0] == "ability" and t[2] in KEY_ABILITIES]

    done: set[tuple[str, str]] = set()
    if OUT_PATH.exists():
        with OUT_PATH.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                done.add((row["kind"], row["name_zh"]))

    targets = [t for t in targets if (t[0], t[1]) not in done]
    if args.limit:
        targets = targets[: args.limit]
    print(f"待抓取 {len(targets)} 条（已存在 {len(done)} 条）", flush=True)

    new_file = not OUT_PATH.exists()
    ok = fail = 0
    with OUT_PATH.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["kind", "name_zh", "name_en", "effect_zh", "source"])
        for index, (kind, name_zh, name_en) in enumerate(targets, 1):
            try:
                effect = extract_effect(fetch_page(name_zh), kind)
                if not effect:
                    raise RuntimeError("页面没有效果段落")
                writer.writerow([kind, name_zh, name_en, effect,
                                 "https://wiki.52poke.com/wiki/" + urllib.parse.quote(name_zh)])
                fh.flush()
                ok += 1
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print(f"[{index}/{len(targets)}] 失败 {kind} {name_zh}: {exc}", flush=True)
            if index % 20 == 0:
                print(f"进度 {index}/{len(targets)}，成功 {ok}，失败 {fail}", flush=True)
            time.sleep(args.delay)

    print(f"完成：成功 {ok}，失败 {fail}，输出 {OUT_PATH}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
