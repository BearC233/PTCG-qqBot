"""宝可梦知识库查询层（PostgreSQL + pg_trgm 模糊匹配）。"""

from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

TYPE_ZH = {
    "normal": "一般", "fire": "火", "water": "水", "electric": "电", "grass": "草",
    "ice": "冰", "fighting": "格斗", "poison": "毒", "ground": "地面", "flying": "飞行",
    "psychic": "超能力", "bug": "虫", "rock": "岩石", "ghost": "幽灵", "dragon": "龙",
    "dark": "恶", "steel": "钢", "fairy": "妖精",
}
METHOD_ZH = {"1": "升级", "2": "遗传", "3": "教学", "4": "招式学习器", "10": "特殊"}

# 口语/简写 -> 标准属性名（"鬼龙属性" 这类说法）
TYPE_ALIAS = {
    "鬼": "幽灵", "灵": "幽灵", "ghost": "幽灵",
    "超能": "超能力", "psychic": "超能力",
    "仙": "妖精", "妖": "妖精", "fairy": "妖精",
    "岩": "岩石", "rock": "岩石",
    "地": "地面", "ground": "地面",
    "飞": "飞行", "空": "飞行", "flying": "飞行",
    "暗": "恶", "dark": "恶",
    "钢": "钢", "metal": "钢", "steel": "钢",
    "虫": "虫", "bug": "虫",
    "冰": "冰", "ice": "冰",
    "龙": "龙", "dragon": "龙",
    "电": "电", "electric": "电",
    "炎": "火", "fire": "火",
    "水": "水", "water": "水",
    "草": "草", "grass": "草",
    "毒": "毒", "poison": "毒",
    "普": "一般", "normal": "一般",
    "斗": "格斗", "fighting": "格斗",
}


class Pokedex:
    def __init__(self, pool: asyncpg.Pool, max_results: int = 5) -> None:
        self.pool = pool
        self.max_results = max(max_results, 1)

    # ---------------- 名称解析 ----------------

    async def _resolve(self, conn: asyncpg.Connection, name: str) -> int | None:
        key = (name or "").strip().lower()
        if not key:
            return None
        row = await conn.fetchrow("SELECT pokemon_id FROM kb_alias WHERE alias = $1", key)
        if row:
            return row["pokemon_id"]
        row = await conn.fetchrow(
            "SELECT pokemon_id, similarity(alias, $1) AS score FROM kb_alias "
            "WHERE alias % $1 ORDER BY score DESC LIMIT 1",
            key,
        )
        if row and row["score"] >= 0.35:
            return row["pokemon_id"]
        return None

    # ---------------- 宝可梦档案 ----------------

    async def get_pokemon(self, name: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            pid = await self._resolve(conn, name)
            if pid is None:
                return None
            row = await conn.fetchrow("SELECT * FROM kb_pokemon WHERE id = $1", pid)
            if row is None:
                return None

            abilities = await conn.fetch(
                "SELECT ability_zh, is_hidden FROM kb_pokemon_ability WHERE pokemon_id = $1 ORDER BY is_hidden, ability_zh",
                pid,
            )
            prev_evo = await conn.fetchrow(
                """
                SELECT e.trigger, e.min_level, e.item, e.condition, p.name_zh, p.name_en, p.is_default
                FROM kb_evolution e
                JOIN kb_pokemon p ON p.species_id = e.from_species
                WHERE e.to_species = $1
                ORDER BY p.is_default DESC, p.id LIMIT 1
                """,
                row["species_id"],
            )
            next_evo = await conn.fetch(
                """
                SELECT e.trigger, e.min_level, e.item, e.condition, p.name_zh, p.name_en, p.is_default
                FROM kb_evolution e
                JOIN kb_pokemon p ON p.species_id = e.to_species
                WHERE e.from_species = $1 AND p.is_default
                ORDER BY p.id
                """,
                row["species_id"],
            )

        types = [t for t in (row["type1"], row["type2"]) if t]
        return {
            "名称": row["name_zh"] or row["name_en"],
            "英文名": row["name_en"],
            "分类": row["genus"],
            "属性": types,
            "身高": f"{row['height_m']} m" if row["height_m"] else None,
            "体重": f"{row['weight_kg']} kg" if row["weight_kg"] else None,
            "种族值": {
                "HP": row["hp"], "攻击": row["atk"], "防御": row["def"],
                "特攻": row["spa"], "特防": row["spd"], "速度": row["spe"],
                "合计": row["total"],
            },
            "特性": [
                {"名称": a["ability_zh"], "隐藏特性": bool(a["is_hidden"])} for a in abilities
            ],
            "世代": row["generation"],
            "传说/幻之宝可梦": bool(row["is_legendary"] or row["is_mythical"]),
            "进化": {
                "由": self._evo_text(prev_evo) if prev_evo else None,
                "进化为": [self._evo_text(e) for e in next_evo] or None,
            },
        }

    @staticmethod
    def _evo_text(row: asyncpg.Record) -> str:
        name = row["name_zh"] or row["name_en"]
        cond = row["trigger"] or ""
        if row["min_level"]:
            cond = f"{cond} Lv.{row['min_level']}"
        if row["item"]:
            cond = f"{cond}·{row['item']}"
        if row["condition"]:
            cond = f"{cond}·{row['condition']}"
        return f"{name}（{cond.strip('·')}）" if cond else str(name)

    async def search(
        self,
        keyword: str | None = None,
        types: list[str] | None = None,
        type_match: str = "all",
        exclude_types: list[str] | None = None,
        min_hp: int | None = None,
        min_atk: int | None = None,
        min_def: int | None = None,
        min_spa: int | None = None,
        min_spd: int | None = None,
        min_spe: int | None = None,
        min_total: int | None = None,
        generation: int | None = None,
        legendary: bool | None = None,
        ability: str | None = None,
        move: str | None = None,
        sort_by: str = "total",
        order: str = "desc",
        limit: int | None = None,
    ) -> dict[str, Any]:
        """多维度筛选：属性组合 / 种族值门槛 / 世代 / 特性 / 招式 / 传说标记。"""
        limit = max(1, min(int(limit or self.max_results), 30))
        conds: list[str] = ["p.is_default"]
        params: list[Any] = []

        def add(template: str, value: Any) -> None:
            params.append(value)
            conds.append(template.format(n=len(params)))

        if keyword and str(keyword).strip():
            add(
                "(p.name_zh ILIKE '%' || ${n} || '%' OR p.name_en ILIKE '%' || ${n} || '%')",
                str(keyword).strip(),
            )

        norm_types = self._norm_types(types)
        if types and not norm_types:
            return {"matched": 0, "error": f"无法识别的属性: {types}"}
        if norm_types:
            params.append(norm_types)
            n = len(params)
            if (type_match or "all").lower() == "any":
                conds.append(f"(p.type1 = ANY(${n}::text[]) OR p.type2 = ANY(${n}::text[]))")
            else:
                # 同时具备：请求的每个属性都必须在 type1/type2 中出现
                conds.append(
                    f"(SELECT count(*) FROM unnest(ARRAY[p.type1, p.type2]) AS t "
                    f"WHERE t = ANY(${n}::text[])) = cardinality(${n}::text[])"
                )

        norm_ex = self._norm_types(exclude_types)
        if norm_ex:
            params.append(norm_ex)
            n = len(params)
            conds.append(f"NOT (p.type1 = ANY(${n}::text[]) OR p.type2 = ANY(${n}::text[]))")

        for field_name, value in (
            ("hp", min_hp), ("atk", min_atk), ("def", min_def),
            ("spa", min_spa), ("spd", min_spd), ("spe", min_spe), ("total", min_total),
        ):
            if value is not None:
                add(f"p.{field_name} >= ${{n}}", int(value))

        if generation is not None:
            add("p.generation = ${n}", int(generation))

        if legendary is not None:
            conds.append("(p.is_legendary OR p.is_mythical)" if legendary else "NOT (p.is_legendary OR p.is_mythical)")

        if ability and str(ability).strip():
            add(
                "EXISTS (SELECT 1 FROM kb_pokemon_ability a WHERE a.pokemon_id = p.id "
                "AND (a.ability_zh = ${n} OR a.ability = ${n}))",
                str(ability).strip(),
            )

        if move and str(move).strip():
            add(
                "EXISTS (SELECT 1 FROM kb_pokemon_move pm JOIN kb_move m ON m.id = pm.move_id "
                "WHERE pm.pokemon_id = p.id AND (m.name_zh = ${n} OR lower(m.name_en) = lower(${n})))",
                str(move).strip(),
            )

        sort_columns = {
            "total": "p.total", "hp": "p.hp", "atk": "p.atk", "def": "p.def",
            "spa": "p.spa", "spd": "p.spd", "spe": "p.spe", "speed": "p.spe",
        }
        column = sort_columns.get(str(sort_by or "total").lower(), "p.total")
        direction = "ASC" if str(order or "desc").lower() == "asc" else "DESC"

        params.append(limit)
        sql = f"""
            SELECT p.name_zh, p.name_en, p.type1, p.type2,
                   p.hp, p.atk, p.def, p.spa, p.spd, p.spe, p.total, p.generation,
                   p.is_legendary, p.is_mythical,
                   count(*) OVER() AS matched
            FROM kb_pokemon p
            WHERE {' AND '.join(conds)}
            ORDER BY {column} {direction} NULLS LAST, p.id
            LIMIT ${len(params)}
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        if not rows:
            return {"matched": 0, "items": []}
        return {
            "matched": rows[0]["matched"],
            "items": [
                {
                    "名称": r["name_zh"] or r["name_en"],
                    "属性": [t for t in (r["type1"], r["type2"]) if t],
                    "合计": r["total"],
                    "HP": r["hp"], "攻击": r["atk"], "防御": r["def"],
                    "特攻": r["spa"], "特防": r["spd"], "速度": r["spe"],
                    "世代": r["generation"],
                    "传说": bool(r["is_legendary"] or r["is_mythical"]),
                }
                for r in rows
            ],
        }

    # ---------------- 属性克制 ----------------

    async def type_chart(self, type_name: str) -> dict[str, Any] | None:
        target = self._norm_type(type_name)
        if not target:
            return None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT defend, factor FROM kb_type_chart WHERE attack = $1 ORDER BY factor DESC, defend",
                target,
            )
            rows_in = await conn.fetch(
                "SELECT attack, factor FROM kb_type_chart WHERE defend = $1 AND factor <> 1 ORDER BY factor DESC, attack",
                target,
            )
        if not rows:
            return None

        def group(items: list[tuple[str, float]]) -> dict[str, list[str]]:
            out: dict[str, list[str]] = {}
            for name, factor in items:
                label = {2: "效果拔群 x2", 0.5: "效果不佳 x0.5", 0: "无效 x0"}.get(factor, f"x{factor}")
                out.setdefault(label, []).append(name)
            return out

        return {
            "属性": target,
            "作为攻击方": group([(r["defend"], r["factor"]) for r in rows if r["factor"] != 1]),
            "作为防守方": group([(r["attack"], r["factor"]) for r in rows_in]),
        }

    def _norm_type(self, type_name: str) -> str | None:
        key = str(type_name or "").strip().lower()
        if not key:
            return None
        if key in TYPE_ZH.values():
            return key
        if key in TYPE_ALIAS:
            return TYPE_ALIAS[key]
        for suffix in ("属性", "系"):
            if key.endswith(suffix) and len(key) > len(suffix):
                key = key[: -len(suffix)]
                break
        if key in TYPE_ZH.values():
            return key
        return TYPE_ALIAS.get(key) or TYPE_ZH.get(key)

    def _norm_types(self, values: list[str] | None) -> list[str]:
        """把 ["鬼龙"] 这种连写拆成 ["幽灵","龙"]。"""
        out: list[str] = []
        for value in values or []:
            single = self._norm_type(value)
            if single:
                out.append(single)
                continue
            text = str(value).strip()
            if len(text) == 2:
                a, b = self._norm_type(text[0]), self._norm_type(text[1])
                if a and b:
                    out.extend([a, b])
        return list(dict.fromkeys(out))

    # ---------------- 招式 / 特性 ----------------

    @staticmethod
    def _norm_key(text: str) -> str:
        """名称归一化：去空格与间隔号，忽略大小写。"""
        return re.sub(r"[\s·・•.\-]+", "", str(text or "").lower())

    async def get_move(self, name: str) -> dict[str, Any] | None:
        raw = str(name or "").strip()
        key = self._norm_key(raw)
        if not key:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM kb_move
                WHERE name_key = $1 OR lower(name_en) = lower($2)
                ORDER BY (name_key = $1) DESC, id LIMIT 1
                """,
                key, raw,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT *, similarity(name_key, $1) AS score FROM kb_move "
                    "WHERE name_key % $1 ORDER BY score DESC, id LIMIT 1",
                    key,
                )
                if row is not None and row["score"] < 0.4:
                    row = None
        if row is None:
            return None
        result = {
            "招式": row["name_zh"] or row["name_en"],
            "英文名": row["name_en"],
            "属性": row["type"],
            "分类": row["damage_class"],
            "威力": row["power"],
            "命中": row["accuracy"],
            "PP": row["pp"],
            "效果": row["effect_zh"] or row["effect_en"],
        }
        if row["hazard"]:
            result["留钉子"] = f"是（{row['hazard']}）"
        return result

    async def get_ability(self, name: str) -> dict[str, Any] | None:
        raw = str(name or "").strip()
        key = self._norm_key(raw)
        if not key:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM kb_ability
                WHERE name_key = $1 OR lower(name_en) = lower($2)
                ORDER BY (name_key = $1) DESC, id LIMIT 1
                """,
                key, raw,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT *, similarity(name_key, $1) AS score FROM kb_ability "
                    "WHERE name_key % $1 ORDER BY score DESC, id LIMIT 1",
                    key,
                )
                if row is not None and row["score"] < 0.4:
                    row = None
        if row is None:
            return None
        return {
            "特性": row["name_zh"] or row["name_en"],
            "英文名": row["name_en"],
            "效果": row["effect_zh"] or row["effect_en"],
        }

    async def get_item(self, name: str) -> dict[str, Any] | None:
        raw = str(name or "").strip()
        key = self._norm_key(raw)
        if not key:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM kb_item
                WHERE name_key = $1 OR lower(name_en) = lower($2)
                ORDER BY (name_key = $1) DESC, id LIMIT 1
                """,
                key, raw,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT *, similarity(name_key, $1) AS score FROM kb_item "
                    "WHERE name_key % $1 ORDER BY score DESC, id LIMIT 1",
                    key,
                )
                if row is not None and row["score"] < 0.4:
                    row = None
        if row is None:
            return None
        return {
            "道具": row["name_zh"] or row["name_en"],
            "英文名": row["name_en"],
            "分类": row["category"],
            "效果": row["effect_zh"] or row["effect_en"],
        }

    async def get_learnset(self, name: str, limit: int = 60) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            pid = await self._resolve(conn, name)
            if pid is None:
                return None
            row = await conn.fetchrow("SELECT name_zh, name_en FROM kb_pokemon WHERE id = $1", pid)
            moves = await conn.fetch(
                """
                SELECT m.name_zh, m.type, m.damage_class, pm.method_id, pm.level
                FROM kb_pokemon_move pm JOIN kb_move m ON m.id = pm.move_id
                WHERE pm.pokemon_id = $1
                ORDER BY pm.method_id, pm.level NULLS LAST, m.name_zh
                LIMIT $2
                """,
                pid,
                min(limit, 200),
            )
        if not moves:
            return None
        grouped: dict[str, list[str]] = {}
        for m in moves:
            method = METHOD_ZH.get(str(m["method_id"]), "其他")
            label = f"{method} Lv.{m['level']}" if m["level"] else method
            grouped.setdefault(label, []).append(f"{m['name_zh']}({m['type']})")
        return {
            "宝可梦": row["name_zh"] or row["name_en"],
            "招式": grouped,
        }
