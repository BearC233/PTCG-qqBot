"""把知识库暴露成 LLM 可调用的工具（function calling）。"""

from __future__ import annotations

import json
import logging
from typing import Any

from .kb import Pokedex

log = logging.getLogger(__name__)

_SEARCH_PROPERTIES: dict[str, Any] = {
    "types": {
        "type": "array",
        "items": {"type": "string"},
        "description": "属性列表，中英文均可，例如 [\"幽灵\",\"龙\"]",
    },
    "type_match": {
        "type": "string",
        "enum": ["all", "any"],
        "description": "all=同时具备这些属性（默认），any=具备任一即可",
    },
    "exclude_types": {
        "type": "array",
        "items": {"type": "string"},
        "description": "排除这些属性，例如 [\"一般\"]",
    },
    "keyword": {
        "type": "string",
        "description": "名称片段，只在用户只记得名字一部分时使用；条件筛选请用其它参数",
    },
    "min_total": {"type": "integer", "description": "种族值合计下限"},
    "min_hp": {"type": "integer", "description": "HP 下限"},
    "min_atk": {"type": "integer", "description": "攻击下限"},
    "min_def": {"type": "integer", "description": "防御下限"},
    "min_spa": {"type": "integer", "description": "特攻下限"},
    "min_spd": {"type": "integer", "description": "特防下限"},
    "min_spe": {"type": "integer", "description": "速度下限"},
    "generation": {"type": "integer", "description": "世代，1-9"},
    "legendary": {"type": "boolean", "description": "true 只看传说/幻之宝可梦"},
    "ability": {"type": "string", "description": "拥有某个特性，例如 威吓"},
    "move": {"type": "string", "description": "能学会某个招式，例如 十万伏特"},
    "sort_by": {
        "type": "string",
        "enum": ["total", "hp", "atk", "def", "spa", "spd", "spe"],
        "description": "排序字段，默认按种族值合计",
    },
    "order": {"type": "string", "enum": ["desc", "asc"], "description": "排序方向，默认降序"},
    "limit": {"type": "integer", "description": "返回条数，默认 5，最多 30"},
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_pokemon",
            "description": (
                "多维度检索宝可梦，属性组合、种族值门槛、世代、特性、可学招式可任意组合。"
                "凡是「同时具有X和Y属性」「速度超过100的龙系」「会十万伏特且特攻高的」这类条件筛选，"
                "必须使用本工具的结构化参数，不要用 keyword 做模糊匹配。"
            ),
            "parameters": {
                "type": "object",
                "properties": _SEARCH_PROPERTIES,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_pokemon",
            "description": "查询某一只宝可梦的完整档案：属性、种族值、特性、身高体重、进化关系。已知具体名字时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "宝可梦名称，中文或英文，例如 皮卡丘 / pikachu"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_type_effectiveness",
            "description": "查询某个属性的克制关系：作为攻击方打谁效果拔群/无效，作为防守方怕什么。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "属性名，中文或英文，例如 电 / electric"},
                },
                "required": ["type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pokemon_moves",
            "description": "查询一只宝可梦能学会的招式（按升级/遗传/教学/学习器分组）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "宝可梦名称"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 60"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_move_info",
            "description": "查询某个招式的属性、分类（物理/特殊/变化）、威力、命中、PP 和效果说明。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "招式名，中文或英文，例如 十万伏特 / thunderbolt"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ability_info",
            "description": "查询某个特性的效果说明。涉及特性具体效果或倍率时先查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "特性名，中文或英文，例如 湿润之声 / Liquid Voice"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item_info",
            "description": "查询某个道具的效果说明。涉及道具具体效果时先查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "道具名，中文或英文，例如 气势披带 / Focus Sash"},
                },
                "required": ["name"],
            },
        },
    },
]


class PokemonTools:
    def __init__(self, dex: Pokedex) -> None:
        self.dex = dex

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            return None

    async def dispatch(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "search_pokemon":
                data = await self.dex.search(
                    keyword=args.get("keyword"),
                    types=args.get("types") or [],
                    type_match=args.get("type_match") or "all",
                    exclude_types=args.get("exclude_types") or [],
                    min_hp=self._int(args.get("min_hp")),
                    min_atk=self._int(args.get("min_atk")),
                    min_def=self._int(args.get("min_def")),
                    min_spa=self._int(args.get("min_spa")),
                    min_spd=self._int(args.get("min_spd")),
                    min_spe=self._int(args.get("min_spe")),
                    min_total=self._int(args.get("min_total")),
                    generation=self._int(args.get("generation")),
                    legendary=args.get("legendary") if isinstance(args.get("legendary"), bool) else None,
                    ability=args.get("ability"),
                    move=args.get("move"),
                    sort_by=args.get("sort_by") or "total",
                    order=args.get("order") or "desc",
                    limit=self._int(args.get("limit")),
                )
                if not data["items"]:
                    return json.dumps(
                        {"result": "not_found", "message": "没有符合条件的宝可梦，可以放宽条件再试"},
                        ensure_ascii=False,
                    )
                return json.dumps(data, ensure_ascii=False)

            if name == "find_pokemon":
                data = await self.dex.get_pokemon(str(args.get("name", "")))
                return self._dump(data, f"没有找到宝可梦「{args.get('name')}」")
            if name == "get_type_effectiveness":
                data = await self.dex.type_chart(str(args.get("type", "")))
                return self._dump(data, f"没有找到属性「{args.get('type')}」")
            if name == "get_pokemon_moves":
                data = await self.dex.get_learnset(str(args.get("name", "")), int(args.get("limit") or 60))
                return self._dump(data, f"没有找到「{args.get('name')}」的招式数据")
            if name == "get_move_info":
                data = await self.dex.get_move(str(args.get("name", "")))
                return self._dump(data, f"没有找到招式「{args.get('name')}」")
            if name == "get_ability_info":
                data = await self.dex.get_ability(str(args.get("name", "")))
                return self._dump(data, f"没有找到特性「{args.get('name')}」")
            if name == "get_item_info":
                data = await self.dex.get_item(str(args.get("name", "")))
                return self._dump(data, f"没有找到道具「{args.get('name')}」")
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            log.exception("工具 %s 执行失败", name)
            return json.dumps({"error": f"查询失败: {exc}"}, ensure_ascii=False)

    @staticmethod
    def _dump(data: Any, not_found: str) -> str:
        if not data:
            return json.dumps({"result": "not_found", "message": not_found}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)
