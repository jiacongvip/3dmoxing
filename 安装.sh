#!/usr/bin/env bash
# 3D 贴合服务一键安装 / 升级（宝塔 Docker）
# 用法：bash install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

NAME="customdiy_blender"
PORT="${CUSTOMDIY_BLENDER_PORT:-8765}"
ACTION="${1:-install}"
BPY_WHEEL="bpy-4.5.12-cp311-cp311-manylinux_2_28_x86_64.whl"
BPY_SHA256="dd59575d8d8e1bba40d22186de2a5d49d66487b09d7ca16b5d7a577a44cf72dc"
BPY_DIR="$ROOT/vendor-wheels"
BPY_FILE="$BPY_DIR/$BPY_WHEEL"

log() {
  echo "[3D] $*"
}

fail() {
  echo "[3D] 失败：$1" >&2
  exit 1
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    fail "没有 docker compose，请先在宝塔软件商店安装 Docker"
  fi
}

need_docker() {
  command -v docker >/dev/null 2>&1 || fail "未检测到 Docker，请先在宝塔软件商店安装 Docker"
  docker info >/dev/null 2>&1 || fail "Docker 没启动，请在宝塔里先启动 Docker"
}

show_status() {
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker ps -a --filter "name=^${NAME}$" --format '状态：{{.Status}}  端口：{{.Ports}}'
  else
    echo "容器还不存在"
  fi
}

wait_ready() {
  local i
  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wheel_ok() {
  [ -f "$BPY_FILE" ] || return 1
  echo "${BPY_SHA256}  ${BPY_FILE}" | sha256sum -c - >/dev/null 2>&1
}

fetch_url() {
  local url="$1"
  if command -v wget >/dev/null 2>&1; then
    wget -c --timeout=30 --tries=15 --waitretry=5 -O "$BPY_FILE" "$url"
  else
    curl -C - -L --retry 15 --retry-delay 5 --connect-timeout 30 --output "$BPY_FILE" "$url"
  fi
}

download_bpy_wheel() {
  mkdir -p "$BPY_DIR"
  if wheel_ok; then
    log "Blender 组件已在本地，跳过下载"
    return
  fi
  log "下载 Blender 组件（约 370MB，支持断点续传，中断后再跑会接着下）"
  local urls=(
    "https://pypi.tuna.tsinghua.edu.cn/packages/a9/fe/6f17d05d3ff99acb8aa3e77297083ff294de4a8c8fb079ccb9dd7d7b6184/${BPY_WHEEL}"
    "https://mirrors.aliyun.com/pypi/packages/a9/fe/6f17d05d3ff99acb8aa3e77297083ff294de4a8c8fb079ccb9dd7d7b6184/${BPY_WHEEL}"
    "https://mirrors.cloud.tencent.com/pypi/packages/a9/fe/6f17d05d3ff99acb8aa3e77297083ff294de4a8c8fb079ccb9dd7d7b6184/${BPY_WHEEL}"
    "https://files.pythonhosted.org/packages/a9/fe/6f17d05d3ff99acb8aa3e77297083ff294de4a8c8fb079ccb9dd7d7b6184/${BPY_WHEEL}"
  )
  local url
  for url in "${urls[@]}"; do
    echo "    尝试 $url"
    if fetch_url "$url" && wheel_ok; then
      log "下载完成"
      return
    fi
    echo "    这个源不行，换下一个"
  done
  fail "Blender 组件下载失败，请检查服务器网络后重试"
}

write_dockerfile() {
  cat > "$ROOT/Dockerfile" <<'DOCKER'
FROM python:3.11-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libx11-6 \
    libxxf86vm1 \
    libxfixes3 \
    libxi6 \
    libxrender1 \
    libxkbcommon0 \
    libsm6 \
    libice6 \
    libgl1 \
    libegl1 \
    libgomp1 \
    libdbus-1-3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=20 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com"

RUN pip install --no-cache-dir \
    "numpy>=1.26,<2.0" \
    cython \
    requests \
    zstandard

COPY vendor-wheels/bpy-4.5.12-cp311-cp311-manylinux_2_28_x86_64.whl \
    /wheels/bpy-4.5.12-cp311-cp311-manylinux_2_28_x86_64.whl
RUN pip install --no-cache-dir --no-deps \
    /wheels/bpy-4.5.12-cp311-cp311-manylinux_2_28_x86_64.whl \
    && rm -rf /wheels

COPY . /app
RUN rm -rf /app/vendor-wheels

ENV CUSTOMDIY_BLENDER_HOST=0.0.0.0 \
    CUSTOMDIY_BLENDER_PORT=8765 \
    PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["xvfb-run", "-a", "python", "server.py"]
DOCKER
}

install_or_upgrade() {
  log "1/7 检测 Docker"
  need_docker

  log "2/7 检测环境"
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) ;;
    *) fail "当前系统是 $arch，需要 x86_64 服务器" ;;
  esac
  [ -f "$ROOT/docker-compose.yml" ] || fail "找不到 docker-compose.yml，请把整个 clothing-3d-demo 目录上传完整"
  [ -f "$ROOT/Dockerfile" ] || fail "找不到 Dockerfile"
  mkdir -p "$ROOT/output"

  log "3/7 下载 Blender 组件"
  download_bpy_wheel
  write_dockerfile

  log "4/7 停止旧容器"
  compose down --remove-orphans >/dev/null 2>&1 || true
  docker rm -f "$NAME" >/dev/null 2>&1 || true

  log "5/7 构建 / 升级镜像"
  compose build --no-cache

  log "6/7 启动容器"
  compose up -d --force-recreate --remove-orphans
  docker image prune -f >/dev/null 2>&1 || true

  log "7/7 检测服务"
  if wait_ready; then
    log "安装完成"
    echo
    echo "服务地址：http://127.0.0.1:${PORT}"
    echo "后台填这个：个性定制 → 售卖设置 → 3D服务地址 → 测一下"
    echo
    show_status
  else
    echo
    log "容器起来了，但端口 ${PORT} 还没通，最近日志："
    docker logs --tail 80 "$NAME" || true
    fail "请看上面的日志。常见原因：8765 被占用，或内存不够"
  fi
}

case "$ACTION" in
  install|upgrade|update|"")
    install_or_upgrade
    ;;
  stop)
    need_docker
    log "停止容器"
    compose down --remove-orphans
    log "已停止"
    ;;
  restart)
    need_docker
    log "重启容器"
    compose up -d
    wait_ready || fail "重启后服务未就绪"
    log "已就绪  http://127.0.0.1:${PORT}"
    ;;
  status)
    need_docker
    show_status
    curl -fsS "http://127.0.0.1:${PORT}/api/health" && echo || echo "服务未连通"
    ;;
  logs)
    need_docker
    docker logs --tail 200 -f "$NAME"
    ;;
  *)
    echo "用法：bash install.sh"
    echo "      bash install.sh stop"
    echo "      bash install.sh restart"
    echo "      bash install.sh status"
    echo "      bash install.sh logs"
    exit 1
    ;;
esac
