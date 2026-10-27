#!/usr/bin/env bash
# 一键更新知识库：抓取宝可百科缺失效果 -> 导入 PostgreSQL -> 重启 bot
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] 构建镜像"
docker compose build bot

echo "[2/4] 抓取宝可百科效果（已抓过的自动跳过）"
docker cp scripts/fetch_wiki_effects.py qqbot:/tmp/fetch_wiki_effects.py
docker exec -e BOT_ROOT=/app qqbot python /tmp/fetch_wiki_effects.py

echo "[3/4] 导入知识库（导入期间 bot 会短暂停止）"
docker compose stop bot
docker compose run --rm bot python -m app.rebuild_kb

echo "[4/4] 启动 bot"
docker compose up -d bot
