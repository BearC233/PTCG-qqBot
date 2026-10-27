"""把 PokeAPI CSV 数据集导入 PostgreSQL，构建宝可梦知识库。

用法（容器内）：
    docker compose run --rm bot python -m app.import_pokemon
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import asyncpg

from .config import load_settings

log = logging.getLogger("import")
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

CSV_DIR = Path(os.environ.get("CSV_DIR", "/app/data/csv"))
ZH_HANS = "12"
ZH_HANT = "4"

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

DROP TABLE IF EXISTS kb_pokemon_move, kb_move, kb_type_chart, kb_evolution,
                     kb_pokemon_ability, kb_alias, kb_pokemon CASCADE;

CREATE TABLE kb_pokemon (
    id            INTEGER PRIMARY KEY,
    species_id    INTEGER NOT NULL,
    name_en       TEXT    NOT NULL,
    name_zh       TEXT,
    genus         TEXT,
    type1         TEXT,
    type2         TEXT,
    hp            INTEGER, atk INTEGER, def INTEGER,
    spa           INTEGER, spd INTEGER, spe INTEGER,
    total         INTEGER,
    height_m      NUMERIC(5,1),
    weight_kg     NUMERIC(6,1),
    is_legendary  BOOLEAN NOT NULL DEFAULT FALSE,
    is_mythical   BOOLEAN NOT NULL DEFAULT FALSE,
    evolves_from  INTEGER,
    generation    INTEGER,
    is_default    BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_kb_pokemon_zh ON kb_pokemon USING gin (name_zh gin_trgm_ops);
CREATE INDEX idx_kb_pokemon_species ON kb_pokemon (species_id);

CREATE TABLE kb_alias (
    alias      TEXT PRIMARY KEY,
    pokemon_id INTEGER NOT NULL REFERENCES kb_pokemon(id) ON DELETE CASCADE
);
CREATE INDEX idx_kb_alias_trgm ON kb_alias USING gin (alias gin_trgm_ops);

CREATE TABLE kb_pokemon_ability (
    pokemon_id INTEGER NOT NULL,
    ability    TEXT    NOT NULL,
    ability_zh TEXT,
    is_hidden  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE kb_evolution (
    from_species INTEGER NOT NULL,
    to_species   INTEGER NOT NULL,
    trigger      TEXT,
    min_level    INTEGER,
    item         TEXT,
    condition    TEXT
);
CREATE INDEX idx_kb_evo_from ON kb_evolution (from_species);
CREATE INDEX idx_kb_evo_to ON kb_evolution (to_species);

CREATE TABLE kb_type_chart (
    attack TEXT NOT NULL,
    defend TEXT NOT NULL,
    factor REAL NOT NULL,
    PRIMARY KEY (attack, defend)
);

CREATE TABLE kb_move (
    id           INTEGER PRIMARY KEY,
    name_en      TEXT,
    name_zh      TEXT,
    name_key     TEXT,
    type         TEXT,
    damage_class TEXT,
    power        INTEGER,
    pp           INTEGER,
    accuracy     INTEGER,
    effect_en    TEXT,
    effect_zh    TEXT
);
CREATE INDEX idx_kb_move_key ON kb_move USING gin (name_key gin_trgm_ops);

CREATE TABLE kb_ability (
    id        INTEGER PRIMARY KEY,
    name_en   TEXT,
    name_zh   TEXT,
    name_key  TEXT,
    effect_en TEXT,
    effect_zh TEXT
);
CREATE INDEX idx_kb_ability_key ON kb_ability USING gin (name_key gin_trgm_ops);

CREATE TABLE kb_item (
    id        INTEGER PRIMARY KEY,
    name_en   TEXT,
    name_zh   TEXT,
    name_key  TEXT,
    category  TEXT,
    effect_en TEXT,
    effect_zh TEXT
);
CREATE INDEX idx_kb_item_key ON kb_item USING gin (name_key gin_trgm_ops);

CREATE TABLE kb_pokemon_move (
    pokemon_id INTEGER NOT NULL,
    move_id    INTEGER NOT NULL,
    method_id  INTEGER NOT NULL,
    level      INTEGER
);
CREATE INDEX idx_kb_pm_pokemon ON kb_pokemon_move (pokemon_id);
"""

TRIGGER_ZH = {
    "level-up": "等级提升",
    "use-item": "使用道具",
    "trade": "通讯交换",
    "shed": "特殊（脱壳）",
    "spin": "特殊（旋转）",
    "tower-of-darkness": "特殊（恶之塔）",
    "tower-of-waters": "特殊（水之塔）",
    "three-critical-hits": "特殊（三次击中要害）",
    "take-damage": "特殊（受到伤害）",
    "other": "其他方式",
}
DAMAGE_CLASS_ZH = {"1": "变化", "2": "物理", "3": "特殊"}
STAT_FIELDS = {"1": "hp", "2": "atk", "3": "def", "4": "spa", "5": "spd", "6": "spe"}


def rows(name: str) -> Iterable[dict[str, str]]:
    path = CSV_DIR / name
    if not path.exists():
        log.warning("缺少文件 %s，跳过相关字段", path)
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def kb_rows(name: str) -> Iterable[dict[str, str]]:
    path = Path("/app/data/kb") / name
    if not path.exists():
        log.warning("缺少文件 %s，跳过相关字段", path)
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def norm_key(text: str) -> str:
    """名称归一化：去空格/间隔号/横线，忽略大小写，便于模糊匹配。"""
    return re.sub(r"[\s·・•.\-]+", "", (text or "").lower())


def names_by_id(name: str) -> dict[str, str]:
    """local_language_id -> 名称；优先简体中文，其次繁体，最后英文。"""
    zh_hans: dict[str, str] = {}
    zh_hant: dict[str, str] = {}
    english: dict[str, str] = {}
    for row in rows(name):
        key = next((k for k in row if k.endswith("_id") and k != "local_language_id"), None)
        if key is None:
            continue
        ident = row[key]
        lang = row.get("local_language_id", "")
        if lang == ZH_HANS:
            zh_hans[ident] = row["name"].strip()
        elif lang == ZH_HANT:
            zh_hant[ident] = row["name"].strip()
        elif lang == "9":
            english[ident] = row["name"].strip()
    return {**english, **zh_hant, **zh_hans}


async def main() -> None:
    settings = load_settings(os.environ.get("BOT_CONFIG", "config.yaml"))
    dsn = settings.database.dsn or os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        sys.exit("未配置 database.dsn / POSTGRES_DSN")

    conn = await asyncpg.connect(dsn)
    log.info("建表中 ...")
    await conn.execute(SCHEMA)

    species = {r["id"]: r for r in rows("pokemon_species.csv")}
    species_name = names_by_id("pokemon_species_names.csv")
    genus_map: dict[str, str] = {}
    for r in rows("pokemon_species_names.csv"):
        if r.get("local_language_id") == ZH_HANS and r.get("genus"):
            genus_map[r["pokemon_species_id"]] = r["genus"].strip()
    types_zh = names_by_id("type_names.csv")
    abilities_zh = names_by_id("ability_names.csv")
    moves_zh = names_by_id("move_names.csv")
    items_zh = names_by_id("item_names.csv")
    triggers = {r["id"]: r["identifier"] for r in rows("evolution_triggers.csv")}
    type_ident = {r["id"]: r["identifier"] for r in rows("types.csv")}

    # 类型：pokemon_id -> [type, type]
    ptypes: dict[str, list[tuple[int, str]]] = {}
    for r in rows("pokemon_types.csv"):
        tid = r["type_id"]
        ptypes.setdefault(r["pokemon_id"], []).append(
            (int(r["slot"]), types_zh.get(tid, type_ident.get(tid, "")))
        )

    # 种族值
    stats: dict[str, dict[str, int]] = {}
    for r in rows("pokemon_stats.csv"):
        field_name = STAT_FIELDS.get(r["stat_id"])
        if field_name:
            stats.setdefault(r["pokemon_id"], {})[field_name] = int(r["base_stat"])

    # 特性
    ability_ident = {r["id"]: r["identifier"] for r in rows("abilities.csv")}
    pabilities: dict[str, list[tuple[str, str, bool]]] = {}
    for r in rows("pokemon_abilities.csv"):
        ident = ability_ident.get(r["ability_id"], "")
        pabilities.setdefault(r["pokemon_id"], []).append(
            (ident, abilities_zh.get(r["ability_id"], ident), r["is_hidden"] == "1")
        )

    pokemon_rows: list[tuple[Any, ...]] = []
    for r in rows("pokemon.csv"):
        pid = r["id"]
        sid = r["species_id"]
        sp = species.get(sid, {})
        zh = species_name.get(sid)
        ordered_types = [t for _, t in sorted(ptypes.get(pid, []))]
        s = stats.get(pid, {})
        total = sum(s.get(f, 0) for f in ("hp", "atk", "def", "spa", "spd", "spe")) or None
        pokemon_rows.append(
            (
                int(pid),
                int(sid),
                r["identifier"],
                zh,
                genus_map.get(sid),
                ordered_types[0] if ordered_types else None,
                ordered_types[1] if len(ordered_types) > 1 else None,
                s.get("hp"), s.get("atk"), s.get("def"), s.get("spa"), s.get("spd"), s.get("spe"),
                total,
                round(int(r["height"] or 0) / 10, 1),
                round(int(r["weight"] or 0) / 10, 1),
                sp.get("is_legendary") == "1",
                sp.get("is_mythical") == "1",
                int(sp["evolves_from_species_id"]) if sp.get("evolves_from_species_id") else None,
                int(sp["generation_id"]) if sp.get("generation_id") else None,
                r["is_default"] == "1",
            )
        )

    await conn.copy_records_to_table(
        "kb_pokemon",
        records=pokemon_rows,
        columns=[
            "id", "species_id", "name_en", "name_zh", "genus", "type1", "type2",
            "hp", "atk", "def", "spa", "spd", "spe", "total",
            "height_m", "weight_kg", "is_legendary", "is_mythical", "evolves_from", "generation",
            "is_default",
        ],
    )
    log.info("kb_pokemon: %d 行", len(pokemon_rows))

    # 别名：中文名 / 英文名 / 物种 identifier，且只给默认形态建别名
    alias_map: dict[str, int] = {}
    for r in rows("pokemon.csv"):
        if r["is_default"] != "1":
            continue
        pid = int(r["id"])
        for alias in (species_name.get(r["species_id"]), r["identifier"], species.get(r["species_id"], {}).get("identifier")):
            if alias:
                alias_map.setdefault(alias.strip().lower(), pid)

    manual = Path("/app/data/kb/aliases.csv")
    if manual.exists():
        with manual.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                target = (row.get("pokemon") or "").strip().lower()
                alias = (row.get("alias") or "").strip().lower()
                if alias and target in alias_map:
                    alias_map[alias] = alias_map[target]

    await conn.copy_records_to_table(
        "kb_alias", records=[(a, p) for a, p in alias_map.items()], columns=["alias", "pokemon_id"]
    )
    log.info("kb_alias: %d 行", len(alias_map))

    await conn.copy_records_to_table(
        "kb_pokemon_ability",
        records=[(int(pid), *item) for pid, items in pabilities.items() for item in items],
        columns=["pokemon_id", "ability", "ability_zh", "is_hidden"],
    )

    # 进化关系
    evo_rows: list[tuple[Any, ...]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows("pokemon_evolution.csv"):
        if r.get("is_default") not in ("1", None):
            continue
        to_sp = r["evolved_species_id"]
        from_sp = species.get(to_sp, {}).get("evolves_from_species_id")
        if not from_sp or (from_sp, to_sp) in seen:
            continue
        seen.add((from_sp, to_sp))
        item = items_zh.get(r["trigger_item_id"] or "") if r.get("trigger_item_id") else None
        trigger = TRIGGER_ZH.get(triggers.get(r["evolution_trigger_id"], ""), triggers.get(r["evolution_trigger_id"], ""))

        parts: list[str] = []
        if r.get("minimum_happiness"):
            parts.append(f"亲密度≥{r['minimum_happiness']}")
        if r.get("minimum_affection"):
            parts.append(f"友好度≥{r['minimum_affection']}")
        if r.get("time_of_day"):
            parts.append({"day": "白天", "night": "夜晚", "dusk": "黄昏"}.get(r["time_of_day"], r["time_of_day"]))
        if r.get("known_move_id"):
            parts.append(f"学会{moves_zh.get(r['known_move_id'], '特定招式')}")
        if r.get("known_move_type_id"):
            parts.append(f"学会{types_zh.get(r['known_move_type_id'], '特定属性')}属性招式")
        if r.get("held_item_id"):
            parts.append(f"携带{items_zh.get(r['held_item_id'], '特定道具')}")
        if r.get("needs_overworld_rain") == "1":
            parts.append("下雨天气")
        if r.get("turn_upside_down") == "1":
            parts.append("倒置主机")
        if r.get("gender_id"):
            parts.append("性别限定")
        if r.get("location_id"):
            parts.append("特定地点")
        if r.get("party_species_id"):
            parts.append(f"队伍中有{species_name.get(r['party_species_id'], '特定宝可梦')}")
        if r.get("relative_physical_stats") not in (None, ""):
            parts.append({"1": "攻击>防御", "0": "攻击=防御", "-1": "攻击<防御"}.get(r["relative_physical_stats"], "能力对比"))
        if r.get("condition_expression"):
            parts.append(str(r["condition_expression"])[:40])
        condition = "、".join(parts) or None
        evo_rows.append((
            int(from_sp), int(to_sp), trigger,
            int(r["minimum_level"]) if r.get("minimum_level") else None,
            item, condition,
        ))
    await conn.copy_records_to_table(
        "kb_evolution", records=evo_rows, columns=["from_species", "to_species", "trigger", "min_level", "item", "condition"]
    )
    log.info("kb_evolution: %d 行", len(evo_rows))

    # 属性克制
    chart: list[tuple[str, str, float]] = []
    for r in rows("type_efficacy.csv"):
        attack = type_ident.get(r["damage_type_id"])
        defend = type_ident.get(r["target_type_id"])
        if attack and defend:
            chart.append((types_zh.get(r["damage_type_id"], attack), types_zh.get(r["target_type_id"], defend),
                          int(r["damage_factor"]) / 100))
    await conn.copy_records_to_table("kb_type_chart", records=chart, columns=["attack", "defend", "factor"])
    log.info("kb_type_chart: %d 行", len(chart))

    # 效果文本：宝可百科中文优先，PokéAPI 英文兜底
    wiki_move_effects: dict[str, str] = {}
    wiki_ability_effects: dict[str, str] = {}
    for r in kb_rows("wiki_effects.csv"):
        effect = clean_zh(r.get("effect_zh") or "")
        if not effect:
            continue
        target = wiki_move_effects if r.get("kind") == "move" else wiki_ability_effects
        for key in (norm_key(r.get("name_zh", "")), norm_key(r.get("name_en", ""))):
            if key:
                target[key] = effect
    log.info("百科效果：招式 %d，特性 %d", len(wiki_move_effects), len(wiki_ability_effects))

    move_effect_en = {
        r["move_effect_id"]: (r.get("short_effect") or "").strip()
        for r in rows("move_effect_prose.csv")
        if r.get("local_language_id") == "9"
    }
    move_flavor_zh: dict[str, tuple[int, str]] = {}
    for r in rows("move_flavor_text.csv"):
        if r.get("language_id") != "12":
            continue
        text = re.sub(r"\s+", "", r.get("flavor_text") or "")
        vg = int(r.get("version_group_id") or 0)
        if text and (r["move_id"] not in move_flavor_zh or vg >= move_flavor_zh[r["move_id"]][0]):
            move_flavor_zh[r["move_id"]] = (vg, text)

    # 中文数据集的招式说明，作为百科抓取失败时的兜底
    dataset_path = Path("/app/data/kb/move_list_zh.json")
    dataset_items: list[dict[str, Any]] = []
    dataset_desc: dict[str, str] = {}
    if dataset_path.exists():
        dataset_items = json.loads(dataset_path.read_text(encoding="utf-8"))
        for item in dataset_items:
            key = norm_key(item.get("name_zh", ""))
            if key:
                dataset_desc[key] = clean_zh(item.get("description") or "")

    # 招式
    move_rows = []
    for r in rows("moves.csv"):
        mid = r["id"]
        zh = moves_zh.get(mid)
        effect_zh = (
            wiki_move_effects.get(norm_key(zh))
            or wiki_move_effects.get(norm_key(r["identifier"]))
            or dataset_desc.get(norm_key(zh))
            or dataset_desc.get(norm_key(r["identifier"]))
        )
        if not effect_zh and mid in move_flavor_zh:
            effect_zh = move_flavor_zh[mid][1]
        move_rows.append((
            int(mid), r["identifier"], zh, norm_key(zh or r["identifier"]),
            types_zh.get(r["type_id"], type_ident.get(r["type_id"], "")),
            DAMAGE_CLASS_ZH.get(r.get("damage_class_id", ""), None),
            int(r["power"]) if r.get("power") else None,
            int(r["pp"]) if r.get("pp") else None,
            int(r["accuracy"]) if r.get("accuracy") else None,
            move_effect_en.get(r.get("effect_id") or ""),
            effect_zh,
        ))

    # 超极巨招式 PokéAPI 未收录，用中文数据集补齐
    existing_keys = {row[3] for row in move_rows}
    extra_id = 20001
    for item in dataset_items:
        if item.get("category") != "超极巨":
            continue
        zh = (item.get("name_zh") or "").strip()
        key = norm_key(zh)
        if not key or key in existing_keys:
            continue
        move_rows.append((
            extra_id, item.get("name_en") or zh, zh, key,
            item.get("type") or None, "超极巨", None, None, None,
            None,
            wiki_move_effects.get(key) or dataset_desc.get(key) or None,
        ))
        existing_keys.add(key)
        extra_id += 1

    await conn.copy_records_to_table(
        "kb_move", records=move_rows,
        columns=[
            "id", "name_en", "name_zh", "name_key", "type", "damage_class",
            "power", "pp", "accuracy", "effect_en", "effect_zh",
        ],
    )
    log.info("kb_move: %d 行", len(move_rows))

    # 特性
    ability_effect_en = {
        r["ability_id"]: (r.get("short_effect") or "").strip()
        for r in rows("ability_prose.csv")
        if r.get("local_language_id") == "9"
    }
    ability_flavor_zh: dict[str, tuple[int, str]] = {}
    for r in rows("ability_flavor_text.csv"):
        if r.get("language_id") != "12":
            continue
        text = re.sub(r"\s+", "", r.get("flavor_text") or "")
        vg = int(r.get("version_group_id") or 0)
        if text and (r["ability_id"] not in ability_flavor_zh or vg >= ability_flavor_zh[r["ability_id"]][0]):
            ability_flavor_zh[r["ability_id"]] = (vg, text)

    ability_rows = []
    for r in rows("abilities.csv"):
        aid = r["id"]
        zh = abilities_zh.get(aid)
        effect_zh = (
            wiki_ability_effects.get(norm_key(zh))
            or wiki_ability_effects.get(norm_key(r["identifier"]))
        )
        if not effect_zh and aid in ability_flavor_zh:
            effect_zh = ability_flavor_zh[aid][1]
        ability_rows.append((
            int(aid), r["identifier"], zh, norm_key(zh or r["identifier"]),
            ability_effect_en.get(aid), effect_zh,
        ))
    await conn.copy_records_to_table(
        "kb_ability", records=ability_rows,
        columns=["id", "name_en", "name_zh", "name_key", "effect_en", "effect_zh"],
    )
    log.info("kb_ability: %d 行", len(ability_rows))

    # 学习机（只取最新版本组，避免 60 万行冗余）
    # 最新版本组（32）只有 mastery 标记没有方法与等级，改用猩红/紫（25）
    preferred = os.environ.get("POKEMON_VG", "25")
    learn_rows: list[dict[str, str]] = list(rows("pokemon_moves.csv"))
    groups = {r["version_group_id"] for r in learn_rows}
    latest = preferred if preferred in groups else max(groups, key=int, default=None)
    learn = [
        (int(r["pokemon_id"]), int(r["move_id"]), int(r["pokemon_move_method_id"]),
         int(r["level"]) if r["level"] else None)
        for r in learn_rows if r["version_group_id"] == latest
    ]
    await conn.copy_records_to_table(
        "kb_pokemon_move", records=sorted(set(learn)),
        columns=["pokemon_id", "move_id", "method_id", "level"],
    )
    log.info("kb_pokemon_move: %d 行（version_group=%s）", len(set(learn)), latest)

    await conn.close()
    log.info("导入完成")


if __name__ == "__main__":
    asyncio.run(main())
