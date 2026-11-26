#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "请先按 README 安装 .venv 和 bpy"
  exit 1
fi
export CUSTOMDIY_BLENDER_HOST="${CUSTOMDIY_BLENDER_HOST:-0.0.0.0}"
export CUSTOMDIY_BLENDER_PORT="${CUSTOMDIY_BLENDER_PORT:-8765}"
exec .venv/bin/python server.py
