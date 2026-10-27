"""配置加载：YAML + 环境变量替换（${VAR} / ${VAR:-default}）。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), match.group(2) or "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class OneBotConfig:
    ws_url: str = "ws://napcat:3000"
    access_token: str = ""
    max_backoff: float = 30.0


@dataclass
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.8
    max_tokens: int = 800
    timeout: float = 60.0
    retries: int = 1
    system_prompt: str = "你是一个乐于助人的 AI 助手。"
    # 是否允许模型调用知识库工具（部分模型不支持 function calling 会自动降级）
    tools: bool = True
    # 透传给接口的额外参数，例如 {"enable_thinking": False}
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class RateLimitConfig:
    user_per_minute: int = 6
    group_per_minute: int = 15


@dataclass
class BehaviorConfig:
    private_enabled: bool = True
    group_enabled: bool = True
    private_whitelist: list[str] = field(default_factory=list)
    group_whitelist: list[str] = field(default_factory=list)
    user_blacklist: list[str] = field(default_factory=list)
    require_at_in_group: bool = True
    group_trigger_prefixes: list[str] = field(default_factory=list)
    max_reply_chars: int = 500
    max_concurrency: int = 3
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass
class DatabaseConfig:
    dsn: str = ""
    min_pool: int = 1
    max_pool: int = 4


@dataclass
class MemoryConfig:
    history_turns: int = 8
    history_ttl_minutes: int = 120
    max_item_chars: int = 1000


@dataclass
class KBConfig:
    enabled: bool = True
    max_results: int = 5


@dataclass
class Settings:
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    kb: KBConfig = field(default_factory=KBConfig)


def _build(cls, data: dict[str, Any]):
    """按 dataclass 字段做浅层类型转换，忽略未知键。"""
    kwargs: dict[str, Any] = {}
    for f in cls.__dataclass_fields__.values():  # type: ignore[attr-defined]
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name == "rate_limit" and isinstance(value, dict):
            value = _build(RateLimitConfig, value)
        elif isinstance(value, (list, tuple)):
            value = [str(v) for v in value]
        kwargs[f.name] = value
    return cls(**kwargs)


def load_settings(path: str | Path = "config.yaml") -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = _expand(raw)

    settings = Settings(
        onebot=_build(OneBotConfig, raw.get("onebot", {})),
        llm=_build(LLMConfig, raw.get("llm", {})),
        behavior=_build(BehaviorConfig, raw.get("behavior", {})),
        database=_build(DatabaseConfig, raw.get("database", {})),
        memory=_build(MemoryConfig, raw.get("memory", {})),
        kb=_build(KBConfig, raw.get("kb", {})),
    )
    if not settings.llm.api_key:
        settings.llm.api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not settings.database.dsn:
        settings.database.dsn = os.environ.get("POSTGRES_DSN", "").strip()
    return settings
