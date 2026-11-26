#!/usr/bin/env python3
"""Static demo server with an asynchronous Blender generation endpoint."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "assets" / "custom-garment.glb"
BLENDER_SCRIPT = ROOT / "blender_apply_pattern.py"


def blender_python() -> Path:
    env = os.environ.get("CUSTOMDIY_BLENDER_PYTHON", "").strip()
    if env:
        return Path(env)
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return venv_python
    return Path(sys.executable)


BPY_PYTHON = blender_python()
OUTPUT_ROOT = ROOT / "output" / "jobs"
CACHE_ROOT = ROOT / "output" / "cache"
RESULT_CACHE_ROOT = ROOT / "output" / "cache" / "results"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_MODEL_BYTES = 80 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES * 6
DEFAULT_GARMENT_COLOR = "#234c43"
FIT_ALGO_VERSION = "2026-11-05-bounds"
INTERNAL_TOKEN = os.environ.get("CUSTOMDIY_BLENDER_TOKEN", "").strip()
BIND_HOST = os.environ.get("CUSTOMDIY_BLENDER_HOST", "0.0.0.0").strip() or "0.0.0.0"
BIND_PORT = int(os.environ.get("CUSTOMDIY_BLENDER_PORT", "8765"))
PLACEMENT_NAMES = {
    "front": "正面",
    "back": "背部",
    "left_sleeve": "左袖",
    "right_sleeve": "右袖",
    "pants": "下装",
    "pants_back": "下装背面",
}
TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()
BLENDER_LOCK = threading.Lock()


def task_update(task_id: str, **values) -> None:
    with TASK_LOCK:
        TASKS[task_id].update(values)


def bounded_number(value: object, minimum: float, maximum: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}必须是有效数字") from error
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有效数字")
    return max(minimum, min(maximum, number))


def garment_color(value: object) -> str:
    color = DEFAULT_GARMENT_COLOR if value is None else str(value).strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("服装颜色必须是六位十六进制颜色")
    return color.lower()


def download_bytes(url: str, max_bytes: int, label: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label}地址不合法")
    request = Request(url, headers={"User-Agent": "customdiy-blender"})
    try:
        with urlopen(request, timeout=60) as response:
            data = response.read(max_bytes + 1)
    except URLError as error:
        raise ValueError(f"{label}下载失败") from error
    if not data:
        raise ValueError(f"{label}为空")
    if len(data) > max_bytes:
        raise ValueError(f"{label}超过大小限制")
    return data


def cached_model_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    folder = CACHE_ROOT / digest
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "model.glb"
    if target.is_file() and target.stat().st_size > 64:
        return target
    target.write_bytes(download_bytes(url, MAX_MODEL_BYTES, "底模"))
    return target


def result_cache_key(base_model_url: str, color: str, patterns: list[dict], fit_garment: bool = False) -> str:
    """Only URL-based inputs are cacheable; base64 uploads always regenerate."""
    if not base_model_url:
        return ""
    normalized = []
    for item in patterns:
        image_url = str(item.get("image_url") or "").strip()
        if not image_url:
            return ""
        normalized.append(
            {
                "placement": str(item.get("placement") or ""),
                "image_url": image_url,
                "base_image_url": str(item.get("base_image_url") or "").strip(),
                "overlay_image_url": str(item.get("overlay_image_url") or "").strip(),
                "mask_image_url": str(item.get("mask_image_url") or "").strip(),
                "materials_only": int(item.get("materials_only") or 0),
                "frame": str(item.get("frame") or "").strip(),
                "scale": round(float(item.get("scale", 1.0) or 1.0), 3),
                "position_x": item.get("position_x"),
                "position_y": item.get("position_y"),
            }
        )
    normalized.sort(key=lambda entry: entry["placement"])
    payload = json.dumps(
        {
            "model": base_model_url,
            "color": color,
            "patterns": normalized,
            "fit": bool(fit_garment),
            "algo": FIT_ALGO_VERSION,
            "v": 11,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_result_path(cache_key: str) -> Path | None:
    if not cache_key:
        return None
    target = RESULT_CACHE_ROOT / cache_key / "model.glb"
    if target.is_file() and target.stat().st_size > 64:
        return target
    return None


def store_result_cache(cache_key: str, output_path: Path) -> None:
    if not cache_key or not output_path.is_file():
        return
    folder = RESULT_CACHE_ROOT / cache_key
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(output_path, folder / "model.glb")


def decode_pattern_image(pattern_payload: dict) -> tuple[bytes, str]:
    image_url = str(pattern_payload.get("image_url") or "").strip()
    if image_url:
        raw_image = download_bytes(image_url, MAX_IMAGE_BYTES, "图案")
        suffix = Path(urlparse(image_url).path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            suffix = ".png"
        if suffix == ".jpeg":
            suffix = ".jpg"
        return raw_image, suffix
    image_data = pattern_payload.get("image_data", "")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,(.+)", str(image_data), re.DOTALL)
    if not match:
        raise ValueError("仅支持 PNG、JPG、WebP 图案或图片链接")
    raw_image = base64.b64decode(match.group(2), validate=True)
    if not raw_image or len(raw_image) > MAX_IMAGE_BYTES:
        raise ValueError("单张图案不能超过 12MB")
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[match.group(1)]
    return raw_image, extension


def optional_pattern_image(url: object) -> tuple[bytes, str] | None:
    image_url = str(url or "").strip()
    if not image_url:
        return None
    # 只有域名根、没有路径的地址不是图片，直接忽略
    if str(urlparse(image_url).path or "").strip("/") == "":
        return None
    try:
        raw_image, suffix = decode_pattern_image({"image_url": image_url})
    except ValueError:
        return None
    # 校验真实图片文件头，网页/JSON 一律丢掉，不让 Blender 读到坏文件
    if not (
        raw_image.startswith(b"\x89PNG\r\n\x1a\n")
        or raw_image.startswith(b"\xff\xd8\xff")
        or (raw_image[:4] == b"RIFF" and raw_image[8:12] == b"WEBP")
    ):
        return None
    return raw_image, suffix


def resolve_input_model(payload: dict) -> Path:
    url = str(payload.get("base_model_url") or payload.get("model_url") or "").strip()
    if url:
        return cached_model_path(url)
    if MODEL_PATH.is_file():
        return MODEL_PATH
    raise ValueError("缺少白胚 GLB")


def blender_command(args: list[str]) -> list[str]:
    command = [str(BPY_PYTHON), *args]
    # bpy 作为 Python 模块可以完全无头运行，默认不套 xvfb-run。
    # 只有显式设置 CUSTOMDIY_USE_XVFB=1 且没有 DISPLAY 时才包一层（需要镜像里有 xauth）。
    if os.environ.get("DISPLAY") or os.environ.get("CUSTOMDIY_USE_XVFB", "").strip() != "1":
        return command
    xvfb = shutil.which("xvfb-run")
    if xvfb:
        return [xvfb, "-a", "-s", "-screen 0 1024x768x24", *command]
    return command


def generate_model(
    task_id: str,
    patterns: list[dict],
    color: str,
    output_path: Path,
    input_path: Path,
    cache_key: str = "",
    fit_garment: bool = False,
) -> None:
    started = time.monotonic()
    try:
        task_update(task_id, status="processing", progress=12, message="等待 Blender 处理")
        with BLENDER_LOCK:
            placement_names = "、".join(PLACEMENT_NAMES[item["placement"]] for item in patterns)
            task_update(task_id, progress=28, message=f"Blender 正在投射{placement_names}图案")
            command = blender_command(
                [
                    str(BLENDER_SCRIPT),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--garment-color",
                    color,
                ]
            )
            if input_path.resolve() != MODEL_PATH.resolve():
                # 客户上传的白胚不走演示扫描件专用修复，省内存也更快
                command.append("--skip-scan-cleanup")
            if fit_garment:
                command.append("--fit-garment")
            for item in patterns:
                command.extend(
                    [
                        "--pattern",
                        str(item["path"]),
                        "--placement",
                        item["placement"],
                        "--scale",
                        str(item["scale"]),
                        "--position-x",
                        str(item["position_x"]),
                        "--position-y",
                        str(item["position_y"]),
                        "--pattern-base",
                        str(item.get("base_path") or ""),
                        "--pattern-overlay",
                        str(item.get("overlay_path") or ""),
                        "--pattern-mask",
                        str(item.get("mask_path") or ""),
                        "--pattern-materials-only",
                        "1" if item.get("materials_only") else "0",
                        "--pattern-frame",
                        str(item.get("frame") or ""),
                    ]
                )
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=360,
                check=False,
            )
            try:
                (output_path.parent / "blender.log").write_text(
                    (result.stdout or "") + "\n" + (result.stderr or ""),
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                pass

        if result.returncode != 0 or not output_path.is_file():
            detail = (result.stderr or result.stdout or "Blender generation failed")[-3000:]
            raise RuntimeError(detail.strip())

        try:
            store_result_cache(cache_key, output_path)
        except OSError:
            pass
        elapsed = round(time.monotonic() - started, 2)
        task_update(
            task_id,
            status="completed",
            progress=100,
            message="定制 GLB 已生成",
            model_url=f"/output/jobs/{task_id}/model.glb",
            elapsed=elapsed,
            placements=[item["placement"] for item in patterns],
        )
    except Exception as error:  # noqa: BLE001 - task boundary
        task_update(task_id, status="failed", progress=100, message="生成失败", error=str(error))


class DemoHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Internal-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Internal-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def authorized(self) -> bool:
        if not INTERNAL_TOKEN:
            return True
        return self.headers.get("X-Internal-Token", "").strip() == INTERNAL_TOKEN

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(
                {
                    "code": 200,
                    "msg": "success",
                    "data": {
                        "generator_ready": BPY_PYTHON.is_file(),
                        "model_ready": MODEL_PATH.is_file(),
                    },
                }
            )
            return

        if path == "/api/model":
            # 预览模式没有图案时，直接把外部白胚代理成同源文件，避免浏览器跨域拿不到 GLB。
            query = parse_qs(urlparse(self.path).query)
            url = str((query.get("url") or [""])[0]).strip()
            try:
                model_path = cached_model_path(url)
            except ValueError as error:
                self.send_json({"code": 400, "msg": str(error), "data": {}}, HTTPStatus.BAD_REQUEST)
                return
            size = model_path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "model/gltf-binary")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with model_path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile)
            return

        match = re.fullmatch(r"/api/tasks/([a-f0-9]{32})", path)
        if match:
            if not self.authorized():
                self.send_json({"code": 401, "msg": "无权访问", "data": {}}, HTTPStatus.UNAUTHORIZED)
                return
            with TASK_LOCK:
                task = TASKS.get(match.group(1))
                payload = dict(task) if task else None
            if payload is None:
                self.send_json({"code": 404, "msg": "任务不存在", "data": {}}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"code": 200, "msg": "success", "data": payload})
            return

        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if urlparse(self.path).path != "/api/generate":
            self.send_json({"code": 404, "msg": "接口不存在", "data": {}}, HTTPStatus.NOT_FOUND)
            return
        if not self.authorized():
            self.send_json({"code": 401, "msg": "无权访问", "data": {}}, HTTPStatus.UNAUTHORIZED)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("图案文件大小不正确")
            payload = json.loads(self.rfile.read(length))
            pattern_payloads = payload.get("patterns")
            color = garment_color(payload.get("color"))
            if not isinstance(pattern_payloads, list):
                pattern_payloads = [
                    {
                        "placement": payload.get("placement", "front"),
                        "image_data": payload.get("image_data", ""),
                        "image_url": payload.get("image_url", ""),
                        "scale": payload.get("scale", 1.0),
                        "position_x": payload.get("position_x"),
                        "position_y": payload.get("position_y"),
                    }
                ]
            if not 1 <= len(pattern_payloads) <= 6:
                raise ValueError("每次最多生成正面、背部、左右袖、下装和下装背面六个图案")

            base_model_url = str(payload.get("base_model_url") or payload.get("model_url") or "").strip()
            # DIY 整片效果图模式：抠掉背景并把效果图轮廓对齐到衣身；默认的编辑器模式仍是"盖章"小 Logo。
            fit_garment = bool(payload.get("fit_garment"))
            cache_key = result_cache_key(base_model_url, color, pattern_payloads, fit_garment)
            cached_result = cached_result_path(cache_key)
            if cached_result is not None:
                # 同一底模 + 同一颜色 + 同一组效果图，直接返回上次结果，不再跑 Blender。
                task_id = uuid.uuid4().hex
                with TASK_LOCK:
                    TASKS[task_id] = {
                        "task_id": task_id,
                        "status": "completed",
                        "progress": 100,
                        "message": "定制 GLB 已生成（缓存）",
                        "model_url": f"/output/cache/results/{cache_key}/model.glb",
                        "cached": True,
                        "placements": [str(item.get("placement") or "") for item in pattern_payloads],
                    }
                self.send_json(
                    {"code": 202, "msg": "任务已创建", "data": {"task_id": task_id, "cached": True}},
                    HTTPStatus.ACCEPTED,
                )
                return

            # 缓存未命中才去下载底模（可能几十 MB），命中时上面已经直接返回。
            input_path = resolve_input_model(payload)

            decoded_patterns: list[dict] = []
            seen_placements: set[str] = set()
            for pattern_payload in pattern_payloads:
                placement = pattern_payload.get("placement")
                if placement not in PLACEMENT_NAMES or placement in seen_placements:
                    raise ValueError("图案位置必须是唯一的正面、背部、左袖、右袖、下装或下装背面")
                seen_placements.add(placement)
                raw_image, extension = decode_pattern_image(pattern_payload)
                scale = bounded_number(pattern_payload.get("scale", 1.0), 0.55, 1.8, "图案大小")
                default_position_x = 0.4 if placement == "front" else 0.0
                default_position_y = 0.2 if placement == "front" else (0.15 if placement == "back" else 0.0)
                raw_position_x = pattern_payload.get("position_x")
                raw_position_y = pattern_payload.get("position_y")
                position_x = bounded_number(
                    default_position_x if raw_position_x is None else raw_position_x,
                    -1.0,
                    1.0,
                    "左右位置",
                )
                position_y = bounded_number(
                    default_position_y if raw_position_y is None else raw_position_y,
                    -1.0,
                    1.0,
                    "上下位置",
                )
                decoded_patterns.append(
                    {
                        "placement": placement,
                        "raw_image": raw_image,
                        "extension": extension,
                        "scale": scale,
                        "position_x": position_x,
                        "position_y": position_y,
                        "base_image": optional_pattern_image(pattern_payload.get("base_image_url")),
                        "overlay_image": optional_pattern_image(pattern_payload.get("overlay_image_url")),
                        "mask_image": optional_pattern_image(pattern_payload.get("mask_image_url")),
                        "materials_only": int(pattern_payload.get("materials_only") or 0),
                        "frame": str(pattern_payload.get("frame") or "").strip(),
                    }
                )

            task_id = uuid.uuid4().hex
            task_dir = OUTPUT_ROOT / task_id
            task_dir.mkdir(parents=True, exist_ok=False)
            output_path = task_dir / "model.glb"
            patterns: list[dict] = []
            for item in decoded_patterns:
                placement = item["placement"]
                pattern_path = task_dir / f"pattern-{placement}{item['extension']}"
                pattern_path.write_bytes(item["raw_image"])
                extra_paths = {}
                for extra_name in ("base", "overlay", "mask"):
                    extra = item.get(f"{extra_name}_image")
                    extra_path = None
                    if extra:
                        extra_path = task_dir / f"pattern-{placement}-{extra_name}{extra[1]}"
                        extra_path.write_bytes(extra[0])
                    extra_paths[f"{extra_name}_path"] = extra_path
                patterns.append(
                    {
                        "placement": placement,
                        "path": pattern_path,
                        "scale": item["scale"],
                        "position_x": item["position_x"],
                        "position_y": item["position_y"],
                        **extra_paths,
                        "materials_only": item["materials_only"],
                        "frame": item["frame"],
                    }
                )

            with TASK_LOCK:
                TASKS[task_id] = {
                    "task_id": task_id,
                    "status": "pending",
                    "progress": 5,
                    "message": "任务已创建",
                    "placements": [item["placement"] for item in patterns],
                }
            worker = threading.Thread(
                target=generate_model,
                args=(task_id, patterns, color, output_path, input_path, cache_key, fit_garment),
                daemon=True,
            )
            worker.start()
            self.send_json(
                {"code": 202, "msg": "任务已创建", "data": {"task_id": task_id}},
                HTTPStatus.ACCEPTED,
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error) as error:
            self.send_json({"code": 400, "msg": str(error), "data": {}}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    RESULT_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), DemoHandler)
    print(f"Clothing Blender demo: http://{BIND_HOST}:{BIND_PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
