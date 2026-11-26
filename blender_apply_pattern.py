#!/usr/bin/env python3
"""Project an image onto a GLB garment and export a new GLB.

Runs either with the PyPI bpy runtime or Blender's Python:

    python blender_apply_pattern.py --input garment.glb --pattern logo.png --output result.glb
    blender --background --python blender_apply_pattern.py -- --input ...
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
import zlib
from pathlib import Path

import bpy
import bmesh
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

MASK_PATH = Path(__file__).resolve().parent / "assets" / "garment-recolor-mask.png"


def script_arguments() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]
    return sys.argv[1:]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Project a print onto a garment GLB")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--pattern", action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cleanup-only", action="store_true", help="Repair sleeve/underarm assignment without projecting a pattern")
    parser.add_argument(
        "--skip-scan-cleanup",
        action="store_true",
        help="Skip the demo-scan specific repairs (UV recolour mask, forearm/body fixes); use for customer-uploaded GLBs",
    )
    parser.add_argument("--garment-color", default="#234c43", help="Garment tint as a #RRGGBB color")
    parser.add_argument(
        "--placement",
        action="append",
        choices=("front", "back", "left_sleeve", "right_sleeve", "pants", "pants_back"),
    )
    parser.add_argument("--scale", action="append", type=float)
    parser.add_argument("--position-x", action="append", type=float, help="Horizontal offset from -1 to 1")
    parser.add_argument("--position-y", action="append", type=float, help="Vertical offset from -1 to 1")
    parser.add_argument("--center-x", type=float, help="Horizontal fraction in model bounds")
    parser.add_argument("--center-y", type=float, help="Vertical fraction in model bounds")
    parser.add_argument("--grid", type=int, default=48)
    parser.add_argument("--fit-garment", action="store_true", help="Align a whole-piece print silhouette to the garment silhouette (DIY 整片效果图)")
    parser.add_argument("--keep-background", action="store_true", help="Do not strip flat/checkerboard backgrounds from prints")
    parser.add_argument("--pattern-base", action="append", default=[], help="2D 白胚底图，用来扣掉衣服本身只留印花")
    parser.add_argument("--pattern-overlay", action="append", default=[], help="2D 叠加线稿/领口")
    parser.add_argument("--pattern-mask", action="append", default=[], help="2D 蒙版")
    parser.add_argument("--pattern-materials-only", action="append", default=[], help="1=透明底纯素材，不再扣衣")
    parser.add_argument("--pattern-frame", action="append", default=[], help="标定框 x,y,w,h，相对模型包围盒，y 从顶部往下")
    parser.add_argument("--optimize-base", action="store_true", help="将白胚 GLB 压缩为轻量预览版（缩放贴图 + Draco 压缩）")
    parser.add_argument("--max-texture-size", type=int, default=1536, help="轻量白胚最大贴图边长")
    parser.add_argument("--decals-only", action="store_true", help="仅导出贴片网格 GLB，不重复导出原白胚")
    parser.add_argument("--draco", action="store_true", help="导出时启用 Draco 网格压缩")
    return parser.parse_args(script_arguments())


def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_model(path: Path) -> bpy.types.Object:
    result = bpy.ops.import_scene.gltf(filepath=str(path.resolve()))
    if "FINISHED" not in result:
        raise RuntimeError("GLB import failed")

    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError("No mesh found in GLB")
    return max(meshes, key=lambda obj: len(obj.data.polygons))


def local_bounds(obj: bpy.types.Object) -> tuple[Vector, Vector]:
    mesh = obj.data if obj.type == "MESH" else None
    count = len(mesh.vertices) if mesh is not None else 0
    if count:
        # bound_box 在 mesh.transform 之后不会马上变，1M 面的 8K 模型会拿转正前的厚度当身高。
        coords = np.empty(count * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", coords)
        coords = coords.reshape((-1, 3))
        low = coords.min(axis=0)
        high = coords.max(axis=0)
        return Vector(low.tolist()), Vector(high.tolist())
    corners = [Vector(corner) for corner in obj.bound_box]
    minimum = Vector(tuple(min(point[index] for point in corners) for index in range(3)))
    maximum = Vector(tuple(max(point[index] for point in corners) for index in range(3)))
    return minimum, maximum


def stand_model_up(obj: bpy.types.Object) -> str:
    """贴图按 Blender 的 Y 轴当衣服身高。正规 glTF 导入后身高在 Z，要先转回来。"""
    minimum, maximum = local_bounds(obj)
    size = maximum - minimum
    if size.z <= size.y * 1.25:
        print("UP_AXIS", "y")
        return "y"
    obj.data.transform(Matrix.Rotation(math.radians(-90.0), 4, "X"))
    obj.data.update()
    print(
        "UP_AXIS",
        "z_to_y",
        {"before": [round(float(size.x), 4), round(float(size.y), 4), round(float(size.z), 4)]},
    )
    return "z_to_y"


def restore_export_up_axis(axis: str, only_objects: list[bpy.types.Object] | None = None) -> None:
    """导出前转回导入时的姿态。否则 glTF 会再转一次，预览里衣服就横躺着。"""
    if axis != "z_to_y":
        return
    rotation = Matrix.Rotation(math.radians(90.0), 4, "X")
    seen = set()
    targets = only_objects if only_objects is not None else list(bpy.context.scene.objects)
    for obj in targets:
        if obj.type != "MESH":
            continue
        mesh = obj.data
        key = mesh.as_pointer()
        if key in seen:
            continue
        seen.add(key)
        mesh.transform(rotation)
        mesh.update()
    print("UP_AXIS_RESTORED")


def parse_hex_color(value: str) -> tuple[float, float, float, float]:
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Garment color must be a six-digit #RRGGBB value")

    # glTF stores PBR factors in linear-light space, while the UI and API
    # accept the usual sRGB hex notation.  Writing the raw 0-255 values here
    # makes dark colours render visibly washed out (for example navy becomes
    # pale blue), so convert each channel before putting it in the GLB.
    def to_linear(channel: int) -> float:
        srgb = channel / 255.0
        return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4

    return tuple(to_linear(int(value[index : index + 2], 16)) for index in (1, 3, 5)) + (1.0,)


def garment_material_names(target: bpy.types.Object) -> list[str]:
    garment_name_pattern = re.compile(r"shirt|cloth|garment|dress|top|jacket|coat|uniform|polo|tee|衣|服|衫|裙", re.I)
    return [
        material.name
        for material in target.data.materials
        if material is not None and garment_name_pattern.search(material.name)
    ]


def get_base_color_image(target: bpy.types.Object) -> bpy.types.Image | None:
    for material in target.data.materials:
        if material is None or not material.use_nodes:
            continue
        principled = next((node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
        if principled is not None:
            for link in material.node_tree.links:
                if (
                    link.to_node == principled
                    and link.to_socket.name == "Base Color"
                    and link.from_node.type == "TEX_IMAGE"
                    and link.from_node.image is not None
                ):
                    return link.from_node.image
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                return node.image
    return None


def create_texture_tone_sampler(target: bpy.types.Object):
    """Return a small UV sampler for the merged scan's base-colour image.

    The supplied scan stores the person and the polo in one material.  Its UV
    texture still gives us a useful second signal: exposed skin is warm/orange
    while the shirt is mostly neutral grey.  Downscaling a copy keeps this
    check cheap even though the source image is 4K.
    """
    image = get_base_color_image(target)
    if image is None:
        return None

    preview = image.copy()
    try:
        preview.scale(256, 256)
        width, height = preview.size
        pixels = list(preview.pixels[:])
    finally:
        # The sampler owns the copied pixel values, not the Blender image.
        bpy.data.images.remove(preview)

    def sample(u: float, v: float) -> tuple[float, float, float]:
        # Blender's image pixel rows are addressed from the UV bottom edge.
        x = max(0, min(width - 1, int(float(u) * (width - 1) + 0.5)))
        y = max(0, min(height - 1, int(float(v) * (height - 1) + 0.5)))
        offset = (y * width + x) * 4
        return pixels[offset], pixels[offset + 1], pixels[offset + 2]

    return sample


def is_skin_tone(rgb: tuple[float, float, float]) -> bool:
    """Recognise warm skin pixels without treating neutral cloth as skin."""
    red, green, blue = rgb
    return (
        red > 0.14
        and red > green * 1.10
        and green > blue * 1.02
        and red - blue > 0.07
    )


def ensure_side_fill_material(target: bpy.types.Object) -> str:
    """Keep a solid garment material for the underarm fill panels only.

    The demo scan's shirt must stay on the original textured material.  Face
    splits against the black side panels and cuffs create the jagged seams
    the customer keeps sending back.  Recolour happens in UV space instead.
    """
    existing = next(
        (material for material in target.data.materials if material and material.name.startswith("CUSTOM_GARMENT_COLOR")),
        None,
    ) or bpy.data.materials.get("CUSTOM_GARMENT_COLOR")
    if existing is not None:
        existing.use_backface_culling = False
        if existing.name not in {material.name for material in target.data.materials if material}:
            target.data.materials.append(existing)
        return existing.name

    material = bpy.data.materials.new("CUSTOM_GARMENT_COLOR")
    material.use_nodes = True
    material.use_backface_culling = False
    material.diffuse_color = (1.0, 1.0, 1.0, 1.0)
    principled = material.node_tree.nodes.get("Principled BSDF")
    if principled is not None:
        principled.inputs["Base Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        principled.inputs["Roughness"].default_value = 0.72
        principled.inputs["Metallic"].default_value = 0.0
    target.data.materials.append(material)
    print("SIDE_FILL_MATERIAL", material.name)
    return material.name


def create_fallback_garment_material(target: bpy.types.Object) -> str | None:
    return ensure_side_fill_material(target)


def is_garment_region_face(center: Vector) -> bool:
    """Generous shirt volume used only to seed the UV recolour mask."""
    if is_forearm_bleed_face(center):
        return False
    x = abs(center.x)
    y = center.y
    z = abs(center.z)
    if y < 0.53 or y > 1.01 or x > 0.22:
        return False
    shoulder_t = max(0.0, min(1.0, (y - 0.88) / 0.12))
    shoulder_half_width = 0.125 + 0.055 * (1.0 - shoulder_t)
    torso = 0.54 < y < 0.89 and x < 0.175
    shoulder = 0.88 <= y < 1.00 and x < shoulder_half_width
    sleeve = 0.798 < y < 0.96 and 0.088 < x < 0.215 and z < 0.13
    return torso or shoulder or sleeve


def restore_body_to_scan_material(target: bpy.types.Object) -> int:
    """Put every body face back on the photogrammetry albedo."""
    garment_index = next(
        (
            index
            for index, material in enumerate(target.data.materials)
            if material and material.name == "CUSTOM_GARMENT_COLOR"
        ),
        None,
    )
    if garment_index is None:
        return 0
    original_index = 0 if garment_index else 1
    if original_index >= len(target.data.materials) or target.data.materials[original_index] is None:
        return 0
    restored = 0
    for polygon in target.data.polygons:
        if polygon.material_index != garment_index:
            continue
        polygon.material_index = original_index
        restored += 1
    if restored:
        target.data.update()
    print("RESTORE_SCAN_MATERIAL", {"faces": restored})
    return restored


def _rank_filter(mask: np.ndarray, radius: int, use_max: bool) -> np.ndarray:
    values = mask.astype(np.float32)
    padded = np.pad(values, radius, mode="edge")
    height, width = values.shape
    result = padded[radius : radius + height, radius : radius + width].copy()
    merge = np.maximum if use_max else np.minimum
    limit = radius * radius
    for offset_y in range(-radius, radius + 1):
        for offset_x in range(-radius, radius + 1):
            if offset_x * offset_x + offset_y * offset_y > limit:
                continue
            result = merge(
                result,
                padded[radius + offset_y : radius + offset_y + height, radius + offset_x : radius + offset_x + width],
            )
    return result


def _smooth_mask(mask: np.ndarray) -> np.ndarray:
    return _separable_box_blur(mask, radius=3, repeats=1)


def write_mask_png(path: Path, mask: np.ndarray) -> None:
    """Write a standard PNG without Blender's view-transform crushing mid greys."""
    height, width = mask.shape
    pixels = np.clip(np.rint(mask * 255.0), 0, 255).astype(np.uint8)
    # Keep Blender's row-0 = UV v=0 layout so the mask matches the
    # glTF-exported albedo, which also stores v=0 at the first image row.
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        raw.extend(np.repeat(row, 3))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _separable_box_blur(mask: np.ndarray, radius: int, repeats: int = 1) -> np.ndarray:
    values = mask.astype(np.float32)
    height, width = values.shape
    kernel = 2 * radius + 1
    for _ in range(repeats):
        padded = np.pad(values, ((0, 0), (radius, radius)), mode="edge")
        cumulative = np.pad(np.cumsum(padded, axis=1), ((0, 0), (1, 0)))
        values = (cumulative[:, kernel : kernel + width] - cumulative[:, :width]) / kernel
        padded = np.pad(values, ((radius, radius), (0, 0)), mode="edge")
        cumulative = np.pad(np.cumsum(padded, axis=0), ((1, 0), (0, 0)))
        values = (cumulative[kernel : kernel + height, :] - cumulative[:height, :]) / kernel
    return np.clip(values, 0.0, 1.0)


def _paint_uv_bbox(mask: np.ndarray, us: list[float], vs: list[float]) -> None:
    """Mark the UV footprint of one garment triangle."""
    height, width = mask.shape
    xs = [max(0, min(width - 1, int(round(u * (width - 1))))) for u in us]
    ys = [max(0, min(height - 1, int(round(v * (height - 1))))) for v in vs]
    if not xs or not ys:
        return
    x0, x1 = max(0, min(xs) - 1), min(width - 1, max(xs) + 1)
    y0, y1 = max(0, min(ys) - 1), min(height - 1, max(ys) + 1)
    mask[y0 : y1 + 1, x0 : x1 + 1] = 1


def build_garment_recolor_mask(target: bpy.types.Object, path: Path = MASK_PATH) -> Path | None:
    """Build a UV mask from shirt triangles so black trims keep their photo edges."""
    albedo = get_base_color_image(target)
    uv_layer = target.data.uv_layers.active if target.data.uv_layers else None
    if albedo is None or uv_layer is None:
        print("RECOLOR_MASK_SKIPPED", "missing albedo or uvs")
        return None

    source_width, source_height = albedo.size
    factor = max(1, source_width // 2048)
    width = source_width // factor
    height = source_height // factor
    pixels = np.array(albedo.pixels[:], dtype=np.float32).reshape(source_height, source_width, 4)
    rgb = pixels[::factor, ::factor, :3]
    if rgb.shape[0] != height or rgb.shape[1] != width:
        rgb = pixels[: height * factor : factor, : width * factor : factor, :3]
        height, width = rgb.shape[:2]

    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    luma = red * 0.2126 + green * 0.7152 + blue * 0.0722
    chroma = np.maximum(np.maximum(red, green), blue) - np.minimum(np.minimum(red, green), blue)
    skin = (red > 0.14) & (red > green * 1.10) & (green > blue * 1.02) & ((red - blue) > 0.07)
    black = (luma < 0.058) & (chroma < 0.06)
    logo = (blue > red * 1.08) & (blue > green * 1.02) & (chroma > 0.12)
    green_cloth = (green > red * 1.02) & (green > blue * 1.02) & (luma > 0.04) & (luma < 0.36)
    neutral_cloth = (~skin) & (~black) & (~logo) & (luma > 0.046) & (luma < 0.30) & (chroma < 0.14)
    cloth = (~skin) & (~black) & (~logo) & (green_cloth | neutral_cloth)

    coverage = np.zeros((height, width), dtype=np.uint8)
    region_faces = 0
    for polygon in target.data.polygons:
        if not is_garment_region_face(polygon.center):
            continue
        us = [float(uv_layer.data[loop_index].uv.x) for loop_index in polygon.loop_indices]
        vs = [float(uv_layer.data[loop_index].uv.y) for loop_index in polygon.loop_indices]
        _paint_uv_bbox(coverage, us, vs)
        region_faces += 1

    mask = ((coverage > 0) & cloth).astype(np.float32)
    if mask.max() <= 0:
        mask = ((coverage > 0) & (~skin) & (~black)).astype(np.float32)

    # Pull the mask away from photographed black piping before the soft edge.
    trim_guard = _rank_filter(black.astype(np.float32), 2, use_max=True)
    mask = np.where(trim_guard > 0.35, 0.0, mask)
    mask = _rank_filter(mask, 1, use_max=True)
    mask = _rank_filter(mask, 2, use_max=False)
    smooth = _separable_box_blur(mask, radius=5, repeats=1)

    path.parent.mkdir(parents=True, exist_ok=True)
    write_mask_png(path, smooth)
    print(
        "RECOLOR_MASK",
        {
            "path": str(path.resolve()),
            "size": (width, height),
            "faces": region_faces,
            "active_pixels": int((smooth > 0.08).sum()),
            "coverage": round(float(smooth.mean()), 4),
            "cloth": int(cloth.sum()),
        },
    )
    return path


def is_forearm_bleed_face(center: Vector) -> bool:
    """True when a recoloured triangle sits on the exposed forearm, not the sleeve."""
    x = abs(center.x)
    y = center.y
    if y >= 0.828 or x <= 0.100:
        return False
    return (y < 0.818 and x > 0.112) or (y < 0.800 and x > 0.106)


def revert_forearm_garment_bleed(target: bpy.types.Object) -> int:
    """Move sleeve-cuff overspill back onto the scan's original skin material."""
    garment_index = next(
        (index for index, material in enumerate(target.data.materials) if material and material.name == "CUSTOM_GARMENT_COLOR"),
        None,
    )
    if garment_index is None:
        return 0
    original_index = 0 if garment_index else 1
    if original_index >= len(target.data.materials) or target.data.materials[original_index] is None:
        return 0
    reverted = 0
    for polygon in target.data.polygons:
        if polygon.material_index != garment_index:
            continue
        if not is_forearm_bleed_face(polygon.center):
            continue
        polygon.material_index = original_index
        reverted += 1
    if reverted:
        target.data.update()
    print("REVERT_FOREARM_BLEED", {"faces": reverted})
    return reverted


def remove_merged_scan_side_fills() -> None:
    for obj in list(bpy.context.scene.objects):
        if obj.name.startswith("CUSTOM_GARMENT_SIDE_FILL"):
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh and mesh.users == 0:
                bpy.data.meshes.remove(mesh)


def prepare_fallback_garment(target: bpy.types.Object, material_name: str) -> None:
    material = next(
        (item for item in target.data.materials if item is not None and item.name == material_name),
        None,
    )
    if material is not None:
        material.use_backface_culling = False
    revert_forearm_garment_bleed(target)
    restore_body_to_scan_material(target)
    remove_merged_scan_side_fills()
    build_garment_recolor_mask(target)


def create_merged_scan_side_fill(target: bpy.types.Object, material_name: str) -> list[bpy.types.Object]:
    """Close the demo scan's open underarm seams with recessed cloth panels.

    This particular source GLB is a merged photogrammetry scan rather than a
    garment-ready model.  Between each upper arm and the torso its shirt mesh
    is physically open, so recolouring every existing triangle still leaves a
    strip of the page background visible.  These panels sit behind the visible
    scan and use the same garment material.  Existing torso and arm geometry
    hide their edges; only the missing shirt area shows through.
    """
    material = next(
        (item for item in target.data.materials if item is not None and item.name == material_name),
        None,
    )
    if material is None:
        return []

    created: list[bpy.types.Object] = []
    columns = 11
    row_count = 52
    for side in (-1.0, 1.0):
        vertices: list[tuple[float, float, float]] = []
        for row in range(row_count):
            y = 0.60 + row * (0.28 / (row_count - 1))
            q = (y - 0.60) / 0.28
            # Stay inside the torso-arm cleft. The previous fill reached
            # |x|≈0.16 and punched into the inner forearm, which read as a
            # pinched, dirty cuff.
            inner_abs = 0.086 + 0.010 * q
            width_factor = min(1.0, q / 0.14, (1.0 - q) / 0.16)
            side_width = 0.024 + 0.008 * q
            outer_abs = inner_abs + side_width * max(0.22, width_factor)
            inner_z = 0.032 - 0.022 * q
            outer_z = 0.008 - 0.090 * q

            inner = Vector((side * inner_abs, y, inner_z))
            outer = Vector((side * outer_abs, y, outer_z))
            for column in range(columns):
                t = column / (columns - 1)
                point = inner.lerp(outer, t)
                point.z -= 0.007 * math.sin(math.pi * t)
                vertices.append(tuple(point))

        faces: list[tuple[int, int, int, int]] = []
        for row in range(row_count - 1):
            for column in range(columns - 1):
                face = (
                    row * columns + column,
                    row * columns + column + 1,
                    (row + 1) * columns + column + 1,
                    (row + 1) * columns + column,
                )
                a, b, c = (Vector(vertices[index]) for index in face[:3])
                if (b - a).cross(c - a).z < 0:
                    face = (face[3], face[2], face[1], face[0])
                faces.append(face)

        mesh = bpy.data.meshes.new(f"CUSTOM_GARMENT_SIDE_FILL_{'L' if side < 0 else 'R'}_MESH")
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        # Lighting the recessed panel with its steep geometric normal makes a
        # chosen burgundy/navy colour appear almost black.  Use the adjacent
        # shirt's forward normal so it remains the selected garment colour.
        try:
            mesh.normals_split_custom_set([(0.0, 0.0, 1.0)] * len(mesh.loops))
        except (AttributeError, RuntimeError):
            pass
        mesh.materials.append(material)
        for polygon in mesh.polygons:
            polygon.use_smooth = True

        panel = bpy.data.objects.new(
            f"CUSTOM_GARMENT_SIDE_FILL_{'L' if side < 0 else 'R'}",
            mesh,
        )
        panel.matrix_world = target.matrix_world.copy()
        bpy.context.collection.objects.link(panel)
        created.append(panel)

    print("GARMENT_SIDE_FILL", {"objects": len(created), "faces": sum(len(item.data.polygons) for item in created)})
    return created


def fill_merged_scan_side_holes(target: bpy.types.Object, material_name: str) -> int:
    """Fill the two real underarm boundary loops in the merged scan mesh.

    The source model contains several tiny photogrammetry boundaries.  Only
    the long loops in the side-shirt range are the visible underarm opening;
    selecting them by size, height span and side position avoids touching hands,
    sleeves or the intentionally uncoloured hem.
    """
    material_index = next(
        (index for index, material in enumerate(target.data.materials) if material and material.name == material_name),
        None,
    )
    if material_index is None:
        return 0

    bmesh_data = bmesh.new()
    bmesh_data.from_mesh(target.data)
    bmesh_data.verts.ensure_lookup_table()
    bmesh_data.verts.index_update()
    bmesh_data.edges.index_update()

    boundary_edges: list[bmesh.types.BMEdge] = []
    adjacency: dict[int, list[int]] = {}
    for edge in bmesh_data.edges:
        if len(edge.link_faces) != 1:
            continue
        center = (edge.verts[0].co + edge.verts[1].co) * 0.5
        if not (
            0.56 < center.y < 0.82
            and 0.075 < abs(center.x) < 0.19
            and -0.09 < center.z < 0.05
        ):
            continue
        edge_index = len(boundary_edges)
        boundary_edges.append(edge)
        for vertex in edge.verts:
            adjacency.setdefault(vertex.index, []).append(edge_index)

    seen: set[int] = set()
    filled_faces = 0
    for start in range(len(boundary_edges)):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        component: list[int] = []
        vertex_indices: set[int] = set()
        while stack:
            edge_index = stack.pop()
            component.append(edge_index)
            edge = boundary_edges[edge_index]
            vertex_indices.update(vertex.index for vertex in edge.verts)
            for vertex in edge.verts:
                for neighbor in adjacency.get(vertex.index, []):
                    if neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)

        if len(component) < 450:
            continue
        coordinates = [bmesh_data.verts[index].co for index in vertex_indices]
        bounds = (
            min(point.x for point in coordinates),
            max(point.x for point in coordinates),
            min(point.y for point in coordinates),
            max(point.y for point in coordinates),
            min(point.z for point in coordinates),
            max(point.z for point in coordinates),
        )
        is_side_loop = (
            (bounds[0] > 0.09 or bounds[1] < -0.079)
            and bounds[3] - bounds[2] > 0.15
        )
        if not is_side_loop:
            continue
        result = bmesh.ops.holes_fill(
            bmesh_data,
            edges=[boundary_edges[index] for index in component],
            sides=0,
        )
        for face in result.get("faces", []):
            face.material_index = material_index
        filled_faces += len(result.get("faces", []))

    if filled_faces:
        bmesh_data.to_mesh(target.data)
        target.data.update()
    bmesh_data.free()
    print("GARMENT_SIDE_HOLES_FILLED", {"faces": filled_faces})
    return filled_faces


def _propagate_runs(reach: np.ndarray, candidate: np.ndarray) -> np.ndarray:
    """Spread `reach` along axis 1 through contiguous runs of `candidate`."""
    starts = candidate & ~np.pad(candidate[:, :-1], ((0, 0), (1, 0)), constant_values=False)
    run_ids = np.cumsum(starts.ravel()).reshape(candidate.shape)
    ids = np.where(candidate, run_ids, 0)
    hit = np.zeros(int(run_ids.max()) + 1, dtype=bool)
    hit[ids[reach & candidate]] = True
    hit[0] = False
    return hit[ids] & candidate


def flood_from_border(candidate: np.ndarray) -> np.ndarray:
    reach = np.zeros_like(candidate)
    reach[0, :] = candidate[0, :]
    reach[-1, :] = candidate[-1, :]
    reach[:, 0] = candidate[:, 0]
    reach[:, -1] = candidate[:, -1]
    for _ in range(96):
        before = int(reach.sum())
        reach = _propagate_runs(reach, candidate)
        reach = _propagate_runs(reach.T, candidate.T).T
        if int(reach.sum()) == before:
            break
    return reach


def strip_flat_background(
    image: bpy.types.Image,
    output_path: Path,
    preserve_mask: np.ndarray | None = None,
) -> np.ndarray | None:
    """Make a flat or checkerboard background transparent.

    DIY 印刷面效果图经常是画布直接导出的不透明 PNG/JPG：背景要么是纯色，要么把
    透明棋盘格烙进了图里。直接投到衣服上会带一圈灰白框。这里从图片四周取背景
    色板，只把与边缘连通、且颜色接近色板的像素抠成透明，衣服内部同色的文字不受影响。

    Returns the top-down alpha mask (True = opaque) after processing.
    """
    width, height = image.size
    if width < 8 or height < 8:
        return None
    buffer = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    pixels = buffer.reshape(height, width, 4)[::-1]  # top-down
    alpha = pixels[:, :, 3]
    if preserve_mask is not None and preserve_mask.shape == (height, width):
        # The blank-garment layer is the authoritative foreground silhouette.
        # Without it, white artwork touching a white collar can be flood-filled
        # away together with the external white background.
        source_foreground = alpha > 0.06 if float(alpha.min()) < 0.5 else None
        foreground = preserve_mask.astype(bool)
        if source_foreground is not None:
            foreground &= source_foreground
        pixels_out = pixels.copy()
        pixels_out[:, :, 3] = foreground.astype(np.float32)
        save_image_topdown(pixels_out, output_path, image.colorspace_settings.name)
        print("BACKGROUND_MASKED", {"fraction": round(float(foreground.mean()), 4)})
        return foreground
    if float(alpha.min()) < 0.5:
        # 已经带真实透明通道，不需要抠底
        return alpha > 0.06

    rgb = pixels[:, :, :3]
    band = max(3, min(width, height) // 120)
    border = np.concatenate(
        [
            rgb[:band, :, :].reshape(-1, 3),
            rgb[-band:, :, :].reshape(-1, 3),
            rgb[:, :band, :].reshape(-1, 3),
            rgb[:, -band:, :].reshape(-1, 3),
        ]
    )
    quantised = np.clip((border * 15.0).round(), 0, 15).astype(np.int32)
    keys = quantised[:, 0] * 256 + quantised[:, 1] * 16 + quantised[:, 2]
    unique_keys, counts = np.unique(keys, return_counts=True)
    palette: list[np.ndarray] = []
    for key, count in zip(unique_keys, counts, strict=True):
        if count / len(keys) < 0.03:
            continue
        members = border[keys == key]
        palette.append(members.mean(axis=0))
    if not palette:
        return alpha > 0.06

    tolerance = 0.11
    candidate = np.zeros((height, width), dtype=bool)
    for colour in palette:
        candidate |= np.abs(rgb - colour[None, None, :]).max(axis=2) <= tolerance
    reach = flood_from_border(candidate)
    fraction = float(reach.mean())
    if fraction < 0.01 or fraction > 0.9:
        print("BACKGROUND_STRIP_SKIPPED", {"fraction": round(fraction, 4), "palette": len(palette)})
        return alpha > 0.06

    new_alpha = np.where(reach, 0.0, 1.0).astype(np.float32)
    pixels_out = pixels.copy()
    pixels_out[:, :, 3] = new_alpha
    save_image_topdown(pixels_out, output_path, image.colorspace_settings.name)
    print(
        "BACKGROUND_STRIPPED",
        {"fraction": round(fraction, 4), "palette": [tuple(round(float(v), 3) for v in c) for c in palette]},
    )
    return new_alpha > 0.5


class PatternSkip(RuntimeError):
    """No unique print left after subtracting the 2D garment drawing."""


def optional_file(value: str | None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_file() else None


def blender_image_topdown(image: bpy.types.Image) -> np.ndarray:
    width, height = image.size
    buffer = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    return buffer.reshape(height, width, 4)[::-1].copy()


def save_image_topdown(pixels: np.ndarray, output_path: Path, color_space: str) -> None:
    """Write a new RGBA image so Blender cannot drop an added alpha channel."""
    height, width = pixels.shape[:2]
    output = bpy.data.images.new(f"{output_path.stem}-rgba", width=width, height=height, alpha=True)
    try:
        output.colorspace_settings.name = color_space
        output.alpha_mode = "STRAIGHT"
        output.pixels.foreach_set(np.ascontiguousarray(pixels[::-1]).ravel())
        output.filepath_raw = str(output_path.resolve())
        output.file_format = "PNG"
        output.save()
    finally:
        bpy.data.images.remove(output)


def write_image_topdown(image: bpy.types.Image, pixels: np.ndarray, output_path: Path) -> None:
    save_image_topdown(pixels, output_path, image.colorspace_settings.name)


def load_resized_rgba(path: Path, width: int, height: int) -> np.ndarray | None:
    try:
        image = bpy.data.images.load(str(path.resolve()), check_existing=False)
    except RuntimeError as error:
        print("GARMENT_LAYER_UNREADABLE", {"path": str(path), "error": str(error)})
        return None
    try:
        if image.size[0] <= 0 or image.size[1] <= 0:
            print("GARMENT_LAYER_UNREADABLE", {"path": str(path), "error": "empty image"})
            return None
        if tuple(image.size) != (width, height):
            image.scale(width, height)
        return blender_image_topdown(image)
    except RuntimeError as error:
        print("GARMENT_LAYER_UNREADABLE", {"path": str(path), "error": str(error)})
        return None
    finally:
        bpy.data.images.remove(image)


def composite_over(base: np.ndarray, overlay: np.ndarray) -> np.ndarray:
    alpha = overlay[:, :, 3:4]
    out = base.copy()
    out[:, :, :3] = overlay[:, :, :3] * alpha + base[:, :, :3] * (1.0 - alpha)
    out[:, :, 3:4] = np.clip(base[:, :, 3:4] + alpha * (1.0 - base[:, :, 3:4]), 0.0, 1.0)
    return out


def subtract_garment_drawing(
    mockup: bpy.types.Image,
    output_path: Path,
    base_path: Path | None,
    overlay_path: Path | None,
    mask_path: Path | None,
) -> str:
    """扣掉 2D 白胚衣服，只留下客户印花。对齐仍用扣之前的衣服轮廓。"""
    extras = [path for path in (base_path, overlay_path, mask_path) if path is not None]
    if not extras:
        return "keep"
    width, height = mockup.size
    mockup_px = blender_image_topdown(mockup)
    expected = None
    for path in extras:
        layer = load_resized_rgba(path, width, height)
        if layer is None:
            continue
        expected = layer if expected is None else composite_over(expected, layer)
    if expected is None:
        return "keep"
    diff = np.abs(mockup_px[:, :, :3] - expected[:, :, :3]).max(axis=2)
    mockup_luma = mockup_px[:, :, :3] @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    expected_luma = expected[:, :, :3] @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    luma_diff = np.abs(mockup_luma - expected_luma)
    opaque = mockup_px[:, :, 3] > 0.08
    overlap = opaque & (expected[:, :, 3] > 0.08)
    if float(overlap.mean()) > 0.02:
        similar = float(np.mean(diff[overlap] <= 0.09))
        if similar >= 0.985:
            print("GARMENT_DRAWING_IDENTICAL", {"similar": round(similar, 4)})
            return "keep"
    if opaque.any():
        baseline = float(np.median(diff[opaque]))
    else:
        baseline = 0.0
    threshold = max(0.10, baseline + 0.08)
    keep = opaque & ((diff > threshold) | (luma_diff > threshold))
    keep = _rank_filter(keep.astype(np.float32), 1, use_max=True) > 0.35
    kept = float(keep.mean())
    if kept < 0.00005:
        print("GARMENT_DRAWING_SUBTRACT_EMPTY", {"kept": round(kept, 6)})
        return "empty"
    # 先腐蚀掉扣底残留的衣片细线，再膨胀把 Logo 边缘补回来
    keep = _rank_filter(keep.astype(np.float32), 1, use_max=False) > 0.5
    keep = _rank_filter(keep.astype(np.float32), 2, use_max=True) > 0.35
    if float(keep.mean()) < 0.00005:
        print("GARMENT_DRAWING_SUBTRACT_EMPTY", {"kept": 0})
        return "empty"
    print_px = mockup_px.copy()
    print_px[:, :, 3] = np.where(keep, np.maximum(mockup_px[:, :, 3], 0.9), 0.0)
    write_image_topdown(mockup, print_px, output_path)
    print("GARMENT_DRAWING_SUBTRACTED", {"kept": round(float(keep.mean()), 4)})
    return "ok"


def _garment_fit_mask(garment_base_path: Path | None, width: int, height: int) -> np.ndarray | None:
    """Use the blank garment drawing silhouette so print-only logos keep full-frame placement."""
    if garment_base_path is None:
        return None
    garment_base = load_resized_rgba(garment_base_path, width, height)
    if garment_base is None:
        return None
    base_alpha = garment_base[:, :, 3]
    if float(base_alpha.min()) < 0.5:
        return base_alpha > 0.06
    rgb = garment_base[:, :, :3]
    band = max(3, min(width, height) // 120)
    border = np.concatenate(
        [
            rgb[:band, :, :].reshape(-1, 3),
            rgb[-band:, :, :].reshape(-1, 3),
            rgb[:, :band, :].reshape(-1, 3),
            rgb[:, -band:, :].reshape(-1, 3),
        ]
    )
    background = np.median(border, axis=0)
    candidate = np.abs(rgb - background[None, None, :]).max(axis=2) <= 0.08
    reach = flood_from_border(candidate)
    silhouette = ~reach
    fraction = float(silhouette.mean())
    if fraction < 0.05 or fraction > 0.98:
        print("GARMENT_FIT_MASK_FALLBACK", {"fraction": round(fraction, 4)})
        return np.ones((height, width), dtype=bool)
    print("GARMENT_FIT_MASK_FLOOD", {"fraction": round(fraction, 4)})
    return silhouette


def create_pattern_material(
    pattern_path: Path,
    placement: str,
    strip_background: bool = True,
    garment_base_path: Path | None = None,
    garment_overlay_path: Path | None = None,
    garment_mask_path: Path | None = None,
    materials_only: bool = False,
) -> tuple[bpy.types.Material, bpy.types.Image, np.ndarray | None]:
    image = bpy.data.images.load(str(pattern_path.resolve()), check_existing=False)
    image.colorspace_settings.name = "sRGB"
    alpha_mask: np.ndarray | None = None
    pixels_probe = blender_image_topdown(image)
    already_print_only = materials_only or float(pixels_probe[:, :, 3].min()) < 0.45
    if already_print_only:
        # DIY materials-only PNG: keep transparency, do not subtract blank garment again.
        image.alpha_mode = "STRAIGHT"
        width, height = image.size
        fit_mask = _garment_fit_mask(garment_base_path, width, height)
        alpha_mask = fit_mask if fit_mask is not None else (pixels_probe[:, :, 3] > 0.06)
        print("MATERIALS_ONLY_PRINT", {"placement": placement, "fit": fit_mask is not None})
    else:
        if strip_background:
            cut_path = pattern_path.with_name(f"{pattern_path.stem}-cut.png")
            preserve_mask = None
            if garment_base_path is not None:
                width, height = image.size
                garment_base = load_resized_rgba(garment_base_path, width, height)
                if garment_base is not None:
                    base_alpha = garment_base[:, :, 3]
                    if float(base_alpha.min()) < 0.5:
                        preserve_mask = base_alpha > 0.06
            alpha_mask = strip_flat_background(image, cut_path, preserve_mask=preserve_mask)
            if cut_path.is_file():
                bpy.data.images.remove(image)
                image = bpy.data.images.load(str(cut_path.resolve()), check_existing=False)
                image.colorspace_settings.name = "sRGB"
                image.alpha_mode = "STRAIGHT"
        print_path = pattern_path.with_name(f"{pattern_path.stem}-print.png")
        subtract_status = subtract_garment_drawing(
            image,
            print_path,
            garment_base_path,
            garment_overlay_path,
            garment_mask_path,
        )
        if subtract_status == "empty":
            raise PatternSkip("no unique prints after subtracting 2D garment")
        if subtract_status == "ok" and print_path.is_file():
            bpy.data.images.remove(image)
            image = bpy.data.images.load(str(print_path.resolve()), check_existing=False)
            image.colorspace_settings.name = "sRGB"
            image.alpha_mode = "STRAIGHT"
            # After subtract, fit against blank garment silhouette when available.
            width, height = image.size
            fit_mask = _garment_fit_mask(garment_base_path, width, height)
            if fit_mask is not None:
                alpha_mask = fit_mask

    material = bpy.data.materials.new(f"CUSTOM_{placement.upper()}_PATTERN_MATERIAL")
    material.use_nodes = True
    material.diffuse_color = (1.0, 1.0, 1.0, 1.0)

    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    elif hasattr(material, "blend_method"):
        material.blend_method = "BLEND"

    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.interpolation = "Linear"
    texture.extension = "CLIP"
    principled.inputs["Roughness"].default_value = 0.72

    material.node_tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])
    material.node_tree.links.new(texture.outputs["Alpha"], principled.inputs["Alpha"])
    material.node_tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    return material, image, alpha_mask


def cast_silhouette(
    bvh: BVHTree,
    minimum: Vector,
    size: Vector,
    is_front: bool,
    rows: int = 128,
    cols: int = 72,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (hits[row, col] top-down, y per row, x per col) seen from front/back."""
    ray_origin = minimum.z + size.z * 1.08 if is_front else minimum.z - size.z * 0.08
    ray_direction = Vector((0.0, 0.0, -1.0 if is_front else 1.0))
    ray_distance = size.z * 1.3
    ys = np.array([minimum.y + size.y * (1.0 - (row + 0.5) / rows) for row in range(rows)])
    xs = np.array([minimum.x + size.x * ((col + 0.5) / cols) for col in range(cols)])
    hits = np.zeros((rows, cols), dtype=bool)
    for row, y in enumerate(ys):
        for col, x in enumerate(xs):
            location = bvh.ray_cast(Vector((float(x), float(y), ray_origin)), ray_direction, ray_distance)[0]
            hits[row, col] = location is not None
    return hits, ys, xs


def _row_widths(mask: np.ndarray, step: float) -> np.ndarray:
    widths = np.zeros(mask.shape[0], dtype=np.float64)
    for row in range(mask.shape[0]):
        cols = np.flatnonzero(mask[row])
        if len(cols):
            widths[row] = (cols[-1] - cols[0] + 1) * step
    return widths


def _image_silhouette(alpha_mask: np.ndarray | None, image: bpy.types.Image) -> tuple[np.ndarray, int, int]:
    width, height = image.size
    if alpha_mask is None or alpha_mask.shape != (height, width) or not alpha_mask.any():
        return np.ones((height, width), dtype=bool), width, height
    return alpha_mask, width, height


def hit_uv(target: bpy.types.Object, face_index: int) -> tuple[float, float] | None:
    mesh = target.data
    uv_layer = mesh.uv_layers.active
    if uv_layer is None or face_index is None or face_index < 0 or face_index >= len(mesh.polygons):
        return None
    polygon = mesh.polygons[face_index]
    if not polygon.loop_indices:
        return None
    us = [uv_layer.data[loop_index].uv[0] for loop_index in polygon.loop_indices]
    vs = [uv_layer.data[loop_index].uv[1] for loop_index in polygon.loop_indices]
    return sum(us) / len(us), sum(vs) / len(vs)


def _nearest_row(ys: np.ndarray, y: float) -> int:
    return int(np.argmin(np.abs(ys - y)))


def _torso_xs(hits: np.ndarray, ys: np.ndarray, xs: np.ndarray, y: float, fraction: float = 0.6) -> list[float]:
    cols = np.flatnonzero(hits[_nearest_row(ys, y)])
    if len(cols) < 2:
        return []
    left, right = int(cols[0]), int(cols[-1])
    inset = int((right - left) * (1.0 - fraction) / 2.0)
    start, end = left + inset, right - inset
    if end <= start:
        start, end = left, right
    count = min(11, end - start + 1)
    return [float(xs[index]) for index in np.linspace(start, end, count).astype(int)]


def _row_span(hits: np.ndarray, ys: np.ndarray, xs: np.ndarray, y: float) -> float:
    cols = np.flatnonzero(hits[_nearest_row(ys, y)])
    if len(cols) < 2:
        return 0.0
    return float(xs[cols[-1]] - xs[cols[0]])


def _mask_span_at(mask: np.ndarray, fraction_from_top: float) -> tuple[float, float]:
    rows = np.flatnonzero(mask.any(axis=1))
    if not len(rows):
        return 0.0, 0.0
    top, bottom = int(rows[0]), int(rows[-1])
    height = float(bottom - top + 1)
    row = min(bottom, top + int(height * fraction_from_top))
    cols = np.flatnonzero(mask[row])
    if len(cols) < 2:
        return 0.0, height
    return float(cols[-1] - cols[0] + 1), height


def detect_shirt_hem(
    bvh: BVHTree,
    target: bpy.types.Object | None,
    minimum: Vector,
    size: Vector,
    hits: np.ndarray,
    ys: np.ndarray,
    xs: np.ndarray,
    crotch_y: float,
    waist_y: float,
    top_y: float,
    leg_hem_y: float,
    shirt_mask: np.ndarray | None,
) -> tuple[float, str]:
    """Pick the real jersey hem. Narrowest silhouette row is only the last fallback."""
    search_min_y = crotch_y
    search_max_y = waist_y + float(size.y) * 0.05
    origin_z = float(minimum.z + size.z * 1.08)
    direction = Vector((0.0, 0.0, -1.0))
    distance = float(size.z) * 1.3
    measure_y = top_y - 0.35 * (top_y - crotch_y)
    torso_width_3d = _row_span(hits, ys, xs, measure_y)
    torso_width_2d, silhouette_height = _mask_span_at(shirt_mask, 0.35) if shirt_mask is not None else (0.0, 0.0)

    def in_search(hem_y: float) -> bool:
        # 2D 图纸把袖子平摊，3D 袖子下垂，宽高比经常差过 25%。
        # 颜色和翻边已经落在裤裆到收腰之间时，不再用这个比例把真实下摆否掉。
        return crotch_y < hem_y < search_max_y

    def front_sample(x: float, y: float):
        location, normal, index, _distance = bvh.ray_cast(Vector((x, y, origin_z)), direction, distance)
        return location, normal, index

    step = max(float(size.y) / 400.0, 0.001)
    scan_ys = np.arange(search_max_y, search_min_y - step, -step)
    sampler = create_texture_tone_sampler(target) if target is not None else None
    color_hem = None
    if sampler is not None:
        shirt_colors = []
        for y in np.linspace(waist_y + float(size.y) * 0.10, min(top_y, waist_y + float(size.y) * 0.25), 8):
            for x in _torso_xs(hits, ys, xs, float(y)):
                _location, _normal, index = front_sample(x, float(y))
                uv = hit_uv(target, index) if target is not None else None
                if uv is not None:
                    shirt_colors.append(sampler(*uv))
        shorts_colors = []
        for y in np.linspace(leg_hem_y, crotch_y, 6):
            row_cols = np.flatnonzero(hits[_nearest_row(ys, float(y))])
            groups: list[list[int]] = []
            for col in row_cols:
                if not groups or int(col) - groups[-1][-1] > 2:
                    groups.append([int(col)])
                else:
                    groups[-1].append(int(col))
            for group in groups:
                x = float(xs[int(round(sum(group) / len(group)))])
                _location, _normal, index = front_sample(x, float(y))
                uv = hit_uv(target, index) if target is not None else None
                if uv is not None:
                    shorts_colors.append(sampler(*uv))
        if len(shirt_colors) >= 4 and len(shorts_colors) >= 4:
            shirt_rgb = np.median(np.array(shirt_colors), axis=0)
            shorts_rgb = np.median(np.array(shorts_colors), axis=0)
            if float(np.max(np.abs(shirt_rgb - shorts_rgb))) < 0.15:
                print("HEM_COLOR_SKIPPED", {"reason": "similar"})
            else:
                low_run = 0
                low_start = None
                for y in scan_ys:
                    samples = []
                    for x in _torso_xs(hits, ys, xs, float(y)):
                        _location, _normal, index = front_sample(x, float(y))
                        uv = hit_uv(target, index) if target is not None else None
                        if uv is not None:
                            samples.append(sampler(*uv))
                    if not samples:
                        low_run = 0
                        low_start = None
                        continue
                    colors = np.array(samples)
                    closer_to_shirt = np.max(np.abs(colors - shirt_rgb), axis=1) <= np.max(np.abs(colors - shorts_rgb), axis=1)
                    if float(np.mean(closer_to_shirt)) < 0.5:
                        if low_run == 0:
                            low_start = float(y)
                        low_run += 1
                        if low_run >= 4 and low_start is not None:
                            color_hem = low_start
                            break
                    else:
                        low_run = 0
                        low_start = None
        else:
            print("HEM_COLOR_SKIPPED", {"reason": "samples"})
    else:
        print("HEM_COLOR_SKIPPED", {"reason": "texture"})

    if color_hem is not None and in_search(color_hem):
        return color_hem, "color"

    lip_best = None
    lip_score = 0.0
    previous_z: dict[float, float] = {}
    for y in scan_ys:
        xs_here = _torso_xs(hits, ys, xs, float(y))
        if not xs_here:
            continue
        lip_hits = 0
        current_z: dict[float, float] = {}
        for x in xs_here:
            location, normal, _index = front_sample(x, float(y))
            if location is None or normal is None:
                continue
            key = round(x, 2)
            current_z[key] = float(location.z)
            dropped = key in previous_z and previous_z[key] - float(location.z) > float(size.z) * 0.015
            if float(normal.y) < -0.25 or dropped:
                lip_hits += 1
        previous_z = current_z
        score = lip_hits / len(xs_here)
        if score > lip_score:
            lip_score = score
            lip_best = float(y)
    if lip_best is not None and lip_score >= 0.35 and in_search(lip_best):
        return lip_best, "lip"

    if torso_width_3d > 0 and torso_width_2d > 0 and silhouette_height > 0:
        aspect_hem = top_y - torso_width_3d * (silhouette_height / torso_width_2d)
        if in_search(aspect_hem):
            return aspect_hem, "aspect"

    print("HEM_FALLBACK_WAIST", {"waist": round(waist_y, 4)})
    return waist_y, "waist"


def detect_garment_regions(
    bvh: BVHTree,
    minimum: Vector,
    size: Vector,
    target: bpy.types.Object | None = None,
    shirt_mask: np.ndarray | None = None,
) -> dict:
    """Return shirt and shorts spans. Shirt bottom is the real hem, not the narrowest waist row."""
    hits, ys, xs = cast_silhouette(bvh, minimum, size, True, rows=192, cols=96)
    occupied = np.flatnonzero(hits.any(axis=1))
    if not len(occupied):
        return {}
    rows = hits.shape[0]
    mid = hits.shape[1] // 2
    widths = _row_widths(hits, float(size.x) / hits.shape[1])
    crotch = None
    for row in range(rows - 1, -1, -1):
        row_hits = hits[row]
        if row_hits.any() and not row_hits[mid - 1 : mid + 2].any() and row_hits[:mid].any() and row_hits[mid:].any():
            crotch = row
        elif crotch is not None and row_hits[mid - 1 : mid + 2].all():
            break
    if crotch is None:
        return {}
    leg_row = crotch
    leg_ref = float(widths[crotch])
    for row in range(crotch + 1, rows):
        if widths[row] < leg_ref * 0.78:
            leg_row = row
            break
    top_limit = max(0, crotch - int(rows * 0.25))
    waist = top_limit + int(np.argmin(widths[top_limit:crotch])) if crotch > top_limit else crotch
    first = int(occupied[0])
    top_y = float(ys[first])
    waist_y = float(ys[waist])
    crotch_y = float(ys[crotch])
    leg_hem_y = float(ys[leg_row])
    shirt_hem, method = detect_shirt_hem(
        bvh,
        target,
        minimum,
        size,
        hits,
        ys,
        xs,
        crotch_y,
        waist_y,
        top_y,
        leg_hem_y,
        shirt_mask,
    )
    return {
        "top": (float(shirt_hem), top_y),
        "pants": (leg_hem_y, float(shirt_hem)),
        "pants_bottom": leg_hem_y,
        "hem": float(shirt_hem),
        "hem_method": method,
    }


def format_garment_regions(regions: dict) -> dict:
    printable = {}
    for key, value in regions.items():
        if isinstance(value, (tuple, list)) and len(value) == 2:
            printable[key] = tuple(round(float(item), 4) for item in value)
        elif isinstance(value, (int, float)):
            printable[key] = round(float(value), 4)
        else:
            printable[key] = value
    return printable


def load_shirt_mask(path: Path | None) -> np.ndarray | None:
    if path is None or not path.is_file():
        return None
    image = bpy.data.images.load(str(path.resolve()), check_existing=False)
    try:
        width, height = image.size
    finally:
        bpy.data.images.remove(image)
    return _garment_fit_mask(path, width, height)


def parse_placement_frame(value: str) -> dict | None:
    parts = [part.strip() for part in str(value or "").split(",")]
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = [float(part) for part in parts]
    except ValueError:
        return None
    if w <= 0.02 or h <= 0.02:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def placement_region_box(placement: str, frame: dict | None, regions: dict, minimum: Vector, maximum: Vector):
    size = maximum - minimum
    if frame:
        min_x = float(minimum.x) + float(frame["x"]) * float(size.x)
        max_x = min_x + float(frame["w"]) * float(size.x)
        max_y = float(maximum.y) - float(frame["y"]) * float(size.y)
        min_y = max_y - float(frame["h"]) * float(size.y)
        return (min_x, max_x, min_y, max_y)
    key = "pants" if placement in {"pants", "pants_back"} else "top" if placement in {"front", "back"} else ""
    span = regions.get(key) if key else None
    if not span:
        return None
    return (None, None, float(span[0]), float(span[1]))


def fit_frame_to_garment(
    target: bpy.types.Object,
    bvh: BVHTree,
    image: bpy.types.Image,
    alpha_mask: np.ndarray | None,
    placement: str,
    region_box,
    garment_regions: dict | None = None,
) -> tuple[float, float, float, float, float, float] | None:
    """Map the 2D garment silhouette box onto the shirt or shorts region."""
    if region_box is None:
        return None
    minimum, maximum = local_bounds(target)
    size = maximum - minimum
    is_front = placement in {"front", "pants"}
    hits, ys, xs = cast_silhouette(bvh, minimum, size, is_front)
    if not hits.any():
        return None
    box_min_x, box_max_x, region_min_y, region_max_y = region_box
    framed = box_min_x is not None
    if region_max_y < region_min_y:
        region_min_y, region_max_y = region_max_y, region_min_y
    region_rows = [index for index, y in enumerate(ys) if region_min_y <= float(y) <= region_max_y]
    if not region_rows:
        return None
    if box_min_x is None or box_max_x is None:
        region_hits = hits[region_rows]
        cols_any = np.flatnonzero(region_hits.any(axis=0))
        if not len(cols_any):
            return None
        box_min_x = float(xs[cols_any[0]])
        box_max_x = float(xs[cols_any[-1]])
    mask, image_width, image_height = _image_silhouette(alpha_mask, image)
    img_rows = np.flatnonzero(mask.any(axis=1))
    img_cols = np.flatnonzero(mask.any(axis=0))
    if not len(img_rows) or not len(img_cols):
        return None
    img_w = float(img_cols[-1] - img_cols[0] + 1)
    img_h = float(img_rows[-1] - img_rows[0] + 1)
    region_w = float(box_max_x) - float(box_min_x)
    region_h = float(region_max_y) - float(region_min_y)
    if img_w <= 0 or img_h <= 0 or region_w <= 0 or region_h <= 0:
        return None
    regions = garment_regions or {}
    hem_y = regions.get("hem")
    pants_bottom = regions.get("pants_bottom")
    anchor = "region"
    if placement in {"pants", "pants_back"} and not framed and hem_y is not None and pants_bottom is not None:
        measure_row = _nearest_row(ys, float(hem_y) - 0.02)
        measure_cols = np.flatnonzero(hits[measure_row])
        if len(measure_cols) < 2:
            band = [index for index in region_rows if len(np.flatnonzero(hits[index])) >= 2]
            if not band:
                return None
            measure_row = max(
                band,
                key=lambda index: float(xs[np.flatnonzero(hits[index])[-1]] - xs[np.flatnonzero(hits[index])[0]]),
            )
            measure_cols = np.flatnonzero(hits[measure_row])
        width_3d = float(xs[measure_cols[-1]] - xs[measure_cols[0]])
        if width_3d <= 0 or img_w <= 0:
            return None
        sx = sy = width_3d / img_w
        region_center_x = float(xs[measure_cols[0]] + xs[measure_cols[-1]]) / 2.0
        region_min_y = float(pants_bottom) - float(size.y) * 0.01
        region_max_y = float(hem_y)
        # 与上衣同一套「图像第 0 行在上」的换算，把短裤剪影底边钉在裤脚。
        center_y = float(pants_bottom) + (float(img_rows[-1]) - image_height / 2.0) * sy
        anchor = "pants_hem"
    else:
        sx = region_w / img_w
        sy = region_h / img_h
        sx = sy = sy if placement in {"front", "back"} else sx
        region_center_x = (float(box_min_x) + float(box_max_x)) / 2.0
        center_y = None
    pattern_width = image_width * sx
    pattern_height = image_height * sy
    if pattern_width <= 0 or pattern_height <= 0 or not math.isfinite(pattern_width * pattern_height):
        return None
    img_cx = (float(img_cols[0]) + float(img_cols[-1]) + 1.0) / 2.0
    img_cy = (float(img_rows[0]) + float(img_rows[-1]) + 1.0) / 2.0
    region_center_y = (float(region_min_y) + float(region_max_y)) / 2.0
    content_shift_x = (img_cx - image_width / 2.0) * sx
    if placement in {"back", "pants_back"}:
        center_x = region_center_x + content_shift_x
    else:
        center_x = region_center_x - content_shift_x
    if center_y is None:
        center_y = region_center_y + (img_cy - image_height / 2.0) * sy
    print(
        "PATTERN_FIT",
        {
            "placement": placement,
            "anchor": anchor,
            "center": (round(center_x, 4), round(center_y, 4)),
            "size": (round(pattern_width, 4), round(pattern_height, 4)),
            "clip_y": (round(float(region_min_y), 4), round(float(region_max_y), 4)),
        },
    )
    return center_x, center_y, pattern_width, pattern_height, float(region_min_y), float(region_max_y)


def cast_through_hem(
    bvh: BVHTree,
    origin: Vector,
    direction: Vector,
    distance: float,
    *,
    is_front: bool,
    body_depth: float,
) -> tuple[Vector | None, Vector | None, bool]:
    """短裤射线穿过盖在外面的上衣下摆，落到后面的裤片上。

    下摆和短裤是前后两层。第一下打中下摆后，只在很短的厚度里继续找朝向相机的面。
    没有更近的后层面时，仍用第一下，避免把贴片打到身体背面。
    """

    def faces_camera(normal: Vector | None) -> bool:
        if normal is None:
            return False
        return normal.z > 0.08 if is_front else normal.z < -0.08

    location, normal, _index, hit_distance = bvh.ray_cast(origin, direction, distance)
    if location is None:
        return None, None, False
    first_distance = float(hit_distance if hit_distance is not None else (location - origin).length)
    cluster = max(body_depth * 0.16, 0.001)
    best_location, best_normal = (location, normal) if faces_camera(normal) else (None, None)
    traveled = first_distance
    pierced = False
    for _ in range(5):
        step = max(body_depth * 0.004, 0.0003)
        remain = distance - traveled - step
        if remain <= 0.0001:
            break
        start = origin + direction * (traveled + step)
        next_location, next_normal, _next_index, next_distance = bvh.ray_cast(start, direction, remain)
        if next_location is None:
            break
        gap = float(next_distance if next_distance is not None else (next_location - start).length) + step
        if traveled + gap - first_distance > cluster:
            break
        traveled += gap
        if faces_camera(next_normal):
            best_location, best_normal = next_location, next_normal
            pierced = True
    if best_location is None:
        return location, normal, False
    return best_location, best_normal, pierced


def create_projected_decal(
    target: bpy.types.Object,
    material: bpy.types.Material,
    image: bpy.types.Image,
    placement: str,
    scale: float,
    position_x: float,
    position_y: float,
    center_x_fraction: float | None,
    center_y_fraction: float | None,
    grid_size: int,
    alpha_mask: np.ndarray | None = None,
    fit_to_garment: bool = True,
    clip_y_max_override: float | None = None,
    garment_regions: dict | None = None,
    placement_frame: dict | None = None,
    bvh: BVHTree | None = None,
) -> bpy.types.Object:
    minimum, maximum = local_bounds(target)
    size = maximum - minimum
    is_sleeve = placement in {"left_sleeve", "right_sleeve"}
    is_pants = placement in {"pants", "pants_back"}
    is_front_facing = placement in {"front", "pants"}

    if bvh is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bvh = BVHTree.FromObject(target, depsgraph)
    if bvh is None:
        raise RuntimeError("Unable to build garment surface index")

    scale = max(0.55, min(1.8, scale))
    fitted = None
    if fit_to_garment and not is_sleeve and center_x_fraction is None and center_y_fraction is None:
        fitted = fit_frame_to_garment(
            target,
            bvh,
            image,
            alpha_mask,
            placement,
            placement_region_box(placement, placement_frame, garment_regions or {}, minimum, maximum),
            garment_regions,
        )

    clip_y_min: float | None = None
    clip_y_max: float | None = None
    if fitted is not None:
        # 整片效果图已对齐到衣身；用户的位置/缩放参数只做微调
        center_horizontal, center_y, pattern_width, pattern_height, clip_y_min, clip_y_max = fitted
        center_horizontal += position_x * size.x * 0.10 * (1.0 if is_front_facing else -1.0)
        center_y += position_y * size.y * 0.06
        pattern_width *= scale
        pattern_height *= scale
        pattern_width = min(pattern_width, size.x * 1.6)
        pattern_height = min(pattern_height, size.y * 1.2)
    elif is_sleeve:
        horizontal_fraction = 0.32 + position_x * 0.15
        vertical_fraction = 0.70 + position_y * 0.08
        center_horizontal = minimum.z + size.z * horizontal_fraction
        base_height = min(size.y * 0.08, size.x * 0.18)
        base_width = base_height
    elif is_pants:
        # 下装贴在人体包围盒偏下的位置，从正面投射到短裤/裤片。
        horizontal_fraction = 0.5 + position_x * 0.12
        vertical_fraction = 0.28 + position_y * 0.10
        center_horizontal = minimum.x + size.x * (
            center_x_fraction if center_x_fraction is not None else horizontal_fraction
        )
        base_height = size.y * 0.22
        base_width = size.x * 0.40
    else:
        # DIY 印刷面是整片效果图，按衣片铺到胸前/后背，不再当小 Logo 盖章。
        horizontal_fraction = 0.5 + position_x * 0.12 if placement == "front" else 0.5 - position_x * 0.12
        vertical_fraction = 0.64 + position_y * 0.10
        center_horizontal = minimum.x + size.x * (
            center_x_fraction if center_x_fraction is not None else horizontal_fraction
        )
        base_height = size.y * 0.30
        base_width = size.x * 0.50
    if fitted is None:
        center_y = minimum.y + size.y * (
            center_y_fraction if center_y_fraction is not None else vertical_fraction
        )
        image_width, image_height = image.size
        aspect = max(0.45, min(2.4, image_width / max(1, image_height)))
        pattern_height = min(size.y * 0.55, base_height * scale)
        pattern_width = min(size.x * 0.85, max(base_width * scale, pattern_height * aspect))
        if not is_sleeve:
            pattern_height = min(size.y * 0.55, max(pattern_height, pattern_width / max(aspect, 0.45)))
    surface_offset = max(size.z * 0.005, 0.00025)

    grid_size = max(12, min(96, grid_size))
    if fitted is not None:
        grid_size = max(grid_size, 96)
    is_front = is_front_facing
    is_left_sleeve = placement == "left_sleeve"
    if is_sleeve:
        ray_origin = maximum.x + size.x * 0.08 if is_left_sleeve else minimum.x - size.x * 0.08
        ray_direction = Vector((-1.0 if is_left_sleeve else 1.0, 0.0, 0.0))
        ray_distance = size.x * 1.3
    else:
        ray_origin = maximum.z + size.z * 0.08 if is_front else minimum.z - size.z * 0.08
        ray_direction = Vector((0.0, 0.0, -1.0 if is_front else 1.0))
        ray_distance = size.z * 1.3
    vertices: list[tuple[float, float, float]] = []
    uv_by_vertex: list[tuple[float, float]] = []
    grid_indices: list[list[int | None]] = []
    pierced_hits = 0

    for row in range(grid_size + 1):
        v = row / grid_size
        y = center_y + (v - 0.5) * pattern_height
        row_indices: list[int | None] = []
        if (clip_y_max is not None and y > clip_y_max) or (clip_y_min is not None and y < clip_y_min):
            grid_indices.append([None] * (grid_size + 1))
            continue
        for column in range(grid_size + 1):
            u = column / grid_size
            if is_sleeve:
                horizontal = 0.5 - u if is_left_sleeve else u - 0.5
                z = center_horizontal + horizontal * pattern_width
                origin = Vector((ray_origin, y, z))
            else:
                horizontal = u - 0.5 if is_front else 0.5 - u
                x = center_horizontal + horizontal * pattern_width
                origin = Vector((x, y, ray_origin))
            if is_pants:
                location, normal, pierced = cast_through_hem(
                    bvh,
                    origin,
                    ray_direction,
                    ray_distance,
                    is_front=is_front,
                    body_depth=float(size.z),
                )
                if pierced:
                    pierced_hits += 1
            else:
                hit = bvh.ray_cast(origin, ray_direction, ray_distance)
                location, normal = hit[0], hit[1]
            if is_sleeve:
                normal_matches_side = normal is not None and (
                    normal.x > 0.25 if is_left_sleeve else normal.x < -0.25
                )
            else:
                normal_matches_side = normal is not None and (
                    normal.z > 0.08 if is_front else normal.z < -0.08
                )
            if location is None or normal is None or not normal_matches_side:
                row_indices.append(None)
                continue
            projected = location + normal.normalized() * surface_offset
            row_indices.append(len(vertices))
            vertices.append(tuple(projected))
            uv_by_vertex.append((u, v))
        grid_indices.append(row_indices)

    faces: list[tuple[int, ...]] = []
    for row in range(grid_size):
        for column in range(grid_size):
            corners = (
                grid_indices[row][column],
                grid_indices[row][column + 1],
                grid_indices[row + 1][column + 1],
                grid_indices[row + 1][column],
            )
            valid = [index for index in corners if index is not None]
            if len(valid) == 4:
                quad = corners if (is_front or is_sleeve) else (corners[0], corners[3], corners[2], corners[1])
                faces.append(quad)
            elif len(valid) == 3:
                tri = tuple(valid) if (is_front or is_sleeve) else tuple(reversed(valid))
                faces.append(tri)

    if not faces:
        raise RuntimeError("Pattern projection missed the garment surface")

    mesh = bpy.data.meshes.new(f"CUSTOM_{placement.upper()}_PATTERN_MESH")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()

    uv_layer = mesh.uv_layers.new(name="UVMap")
    for polygon in mesh.polygons:
        for loop_index in polygon.loop_indices:
            vertex_index = mesh.loops[loop_index].vertex_index
            uv_layer.data[loop_index].uv = uv_by_vertex[vertex_index]

    decal = bpy.data.objects.new(f"CUSTOM_{placement.upper()}_PATTERN", mesh)
    decal.matrix_world = target.matrix_world.copy()
    decal.data.materials.append(material)
    bpy.context.collection.objects.link(decal)

    print(
        "PATTERN_PROJECTED",
        {
            "vertices": len(vertices),
            "faces": len(faces),
            "placement": placement,
            "center": (round(center_horizontal, 5), round(center_y, 5)),
            "size": (round(pattern_width, 5), round(pattern_height, 5)),
            "pierced_hem": pierced_hits,
        },
    )
    return decal


def patch_glb_garment_color(path: Path, material_names: list[str], color: str) -> None:
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"glTF":
        raise RuntimeError("Invalid GLB output")
    json_length = struct.unpack_from("<I", data, 12)[0]
    json_start = 20
    json_end = json_start + json_length
    document = json.loads(data[json_start:json_end].decode("utf-8").rstrip(" \t\r\n\x00"))
    tint = parse_hex_color(color)
    material_name_set = set(material_names)
    changed = False
    for material in document.get("materials", []):
        if material.get("name") not in material_name_set:
            continue
        pbr = material.setdefault("pbrMetallicRoughness", {})
        pbr["baseColorFactor"] = list(tint)
        material["doubleSided"] = True
        changed = True
    if not changed:
        print("PATCH_GARMENT_COLOR_SKIPPED", material_names)
        return
    encoded_json = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded_json += b" " * ((-len(encoded_json)) % 4)
    rest = data[json_end:]
    output = bytearray(data[:12])
    output.extend(struct.pack("<I4s", len(encoded_json), b"JSON"))
    output.extend(encoded_json)
    output.extend(rest)
    struct.pack_into("<I", output, 8, len(output))
    path.write_bytes(output)


def optimize_base_model(input_path: Path, output_path: Path, max_texture_size: int = 1536) -> None:
    """将原始大白胚压缩为适合手机端秒开的轻量 GLB（限制贴图尺寸 + JPEG + Draco 网格压缩）。"""
    reset_scene()
    import_model(input_path)
    max_dim = max(512, min(4096, int(max_texture_size)))
    scaled_count = 0
    for image in bpy.data.images:
        width, height = int(image.size[0]), int(image.size[1])
        if width <= 0 or height <= 0:
            continue
        longest = max(width, height)
        if longest > max_dim:
            ratio = max_dim / float(longest)
            new_w = max(1, int(round(width * ratio)))
            new_h = max(1, int(round(height * ratio)))
            image.scale(new_w, new_h)
            scaled_count += 1
            print("BASE_IMG_SCALED", image.name, f"{width}x{height}->{new_w}x{new_h}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = bpy.ops.export_scene.gltf(
        filepath=str(output_path.resolve()),
        export_format="GLB",
        export_yup=True,
        export_apply=False,
        export_materials="EXPORT",
        export_image_format="JPEG",
        export_image_quality=82,
        export_draco_mesh_compression_enable=True,
        export_draco_mesh_compression_level=6,
        export_draco_position_quantization=11,
        export_draco_normal_quantization=8,
        export_draco_texcoord_quantization=10,
    )
    if "FINISHED" not in result:
        raise RuntimeError("Base GLB optimization failed")
    print("OPTIMIZE_BASE_DONE", {"scaled_images": scaled_count, "output": str(output_path.resolve())})


def export_decals_only(path: Path, decal_objects: list[bpy.types.Object]) -> None:
    """仅导出贴片网格，不重复导出上百万面的原白胚，速度提升几十倍且产物仅几百 KB。"""
    if not decal_objects:
        raise RuntimeError("No decals generated")
    path.parent.mkdir(parents=True, exist_ok=True)
    for obj in list(bpy.context.scene.objects):
        obj.select_set(False)
    for decal in decal_objects:
        decal.select_set(True)
    bpy.context.view_layer.objects.active = decal_objects[0]
    result = bpy.ops.export_scene.gltf(
        filepath=str(path.resolve()),
        export_format="GLB",
        export_yup=True,
        export_apply=False,
        use_selection=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
        export_draco_mesh_compression_enable=True,
        export_draco_mesh_compression_level=6,
        export_draco_position_quantization=12,
        export_draco_normal_quantization=8,
        export_draco_texcoord_quantization=12,
    )
    if "FINISHED" not in result:
        raise RuntimeError("Decals GLB export failed")


def export_model(path: Path, material_names: list[str], color: str, *, use_draco: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "filepath": str(path.resolve()),
        "export_format": "GLB",
        "export_yup": True,
        "export_apply": False,
        "export_materials": "EXPORT",
        "export_image_format": "AUTO",
    }
    if use_draco:
        kwargs.update(
            {
                "export_draco_mesh_compression_enable": True,
                "export_draco_mesh_compression_level": 6,
                "export_draco_position_quantization": 11,
                "export_draco_normal_quantization": 8,
                "export_draco_texcoord_quantization": 10,
            }
        )
    result = bpy.ops.export_scene.gltf(**kwargs)
    if "FINISHED" not in result:
        raise RuntimeError("GLB export failed")
    patch_glb_garment_color(path, material_names, color)


def main() -> None:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)

    if args.optimize_base:
        optimize_base_model(args.input, args.output, max_texture_size=args.max_texture_size)
        return

    reset_scene()
    target = import_model(args.input)
    up_axis = stand_model_up(target)
    if not args.decals_only:
        remove_merged_scan_side_fills()
        material_name = ensure_side_fill_material(target)
    else:
        material_name = ""
    if args.skip_scan_cleanup or args.decals_only:
        # 客户自己上传的白胚：不做演示扫描件专用的修复。那一步会把整张 4K/8K 贴图
        # 读成 Python 列表（几 GB 内存），在小内存服务器上直接被 OOM 杀掉。
        print("SCAN_CLEANUP_SKIPPED")
    else:
        prepare_fallback_garment(target, material_name)
    print(
        "MODEL_INFO",
        {
            "mesh": target.name,
            "polygons": len(target.data.polygons),
            "bounds": [tuple(round(v, 4) for v in b) for b in local_bounds(target)],
        },
    )
    material_names = [material_name]
    if args.cleanup_only:
        if not material_names:
            raise RuntimeError("No garment material found to clean up")
        restore_export_up_axis(up_axis)
        export_model(args.output, material_names, args.garment_color)
        print("OUTPUT_GLB", args.output.resolve())
        return
    patterns = args.pattern or []
    if not patterns:
        raise ValueError("Each generate run needs at least one --pattern, or pass --cleanup-only")
    placements = args.placement or ["front"] * len(patterns)
    scales = args.scale or [1.0] * len(patterns)
    positions_x = args.position_x or [0.0 for _ in placements]
    positions_y = args.position_y or [0.0 for _ in placements]
    pattern_bases = list(args.pattern_base or [])
    pattern_overlays = list(args.pattern_overlay or [])
    pattern_masks = list(args.pattern_mask or [])
    pattern_materials = list(args.pattern_materials_only or [])
    pattern_frames = list(args.pattern_frame or [])
    while len(pattern_bases) < len(patterns):
        pattern_bases.append("")
    while len(pattern_overlays) < len(patterns):
        pattern_overlays.append("")
    while len(pattern_masks) < len(patterns):
        pattern_masks.append("")
    while len(pattern_materials) < len(patterns):
        pattern_materials.append("0")
    while len(pattern_frames) < len(patterns):
        pattern_frames.append("")
    parameter_lengths = (len(placements), len(scales), len(positions_x), len(positions_y))
    if any(length != len(patterns) for length in parameter_lengths):
        raise ValueError("Each pattern requires one placement, scale, position-x and position-y")
    if len(set(placements)) != len(placements):
        raise ValueError("A placement can only have one pattern")
    for pattern_path, scale, position_x, position_y in zip(
        patterns, scales, positions_x, positions_y, strict=True
    ):
        if not pattern_path.is_file():
            raise FileNotFoundError(pattern_path)
        if not math.isfinite(scale):
            raise ValueError("Scale must be finite")
        if not math.isfinite(position_x) or not -1.0 <= position_x <= 1.0:
            raise ValueError("Position X must be between -1 and 1")
        if not math.isfinite(position_y) or not -1.0 <= position_y <= 1.0:
            raise ValueError("Position Y must be between -1 and 1")
    pattern_entries = list(
        zip(
            patterns,
            placements,
            scales,
            positions_x,
            positions_y,
            pattern_bases,
            pattern_overlays,
            pattern_masks,
            pattern_materials,
            pattern_frames,
            strict=True,
        )
    )
    placement_order = {
        "front": 0,
        "back": 1,
        "left_sleeve": 2,
        "right_sleeve": 3,
        "pants": 4,
        "pants_back": 5,
    }
    pattern_entries.sort(key=lambda item: placement_order.get(item[1], 9))
    minimum, maximum = local_bounds(target)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    region_bvh = BVHTree.FromObject(target, depsgraph)
    shirt_source = None
    for placement_name in ("front", "back"):
        for entry in pattern_entries:
            if entry[1] != placement_name:
                continue
            shirt_source = optional_file(entry[5]) or entry[0]
            break
        if shirt_source is not None:
            break
    garment_regions = (
        detect_garment_regions(
            region_bvh,
            minimum,
            maximum - minimum,
            target,
            load_shirt_mask(shirt_source),
        )
        if region_bvh is not None
        else {}
    )
    print("GARMENT_REGIONS", format_garment_regions(garment_regions))
    decal_objects: list[bpy.types.Object] = []
    for (
        pattern_path,
        placement,
        scale,
        position_x,
        position_y,
        garment_base,
        garment_overlay,
        garment_mask,
        materials_only,
        frame_text,
    ) in pattern_entries:
        try:
            material, image, alpha_mask = create_pattern_material(
                pattern_path,
                placement,
                strip_background=not args.keep_background,
                garment_base_path=optional_file(garment_base),
                garment_overlay_path=optional_file(garment_overlay),
                garment_mask_path=optional_file(garment_mask),
                materials_only=str(materials_only) == "1",
            )
            decal = create_projected_decal(
                target,
                material,
                image,
                placement,
                scale,
                position_x,
                position_y,
                args.center_x,
                args.center_y,
                args.grid,
                alpha_mask=alpha_mask,
                fit_to_garment=args.fit_garment,
                garment_regions=garment_regions,
                placement_frame=parse_placement_frame(frame_text),
                bvh=region_bvh,
            )
            decal_objects.append(decal)
        except PatternSkip as error:
            print("PATTERN_SKIPPED", {"placement": placement, "error": str(error)})
            continue
        except RuntimeError as error:
            # 有的白胚没有下装网格，跳过下装不影响胸前/后背预览。
            if placement in {"pants", "pants_back"}:
                print("PATTERN_SKIPPED", {"placement": placement, "error": str(error)})
                continue
            raise
    if args.decals_only:
        restore_export_up_axis(up_axis, only_objects=decal_objects)
        export_decals_only(args.output, decal_objects)
    else:
        restore_export_up_axis(up_axis)
        export_model(args.output, material_names, args.garment_color, use_draco=args.draco)
    print("OUTPUT_GLB", args.output.resolve())


if __name__ == "__main__":
    main()
