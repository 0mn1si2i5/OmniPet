from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

from omnipet._vendor.hatch.scripts import inspect_frames as inspect_vendor
from omnipet._vendor.hatch.scripts import make_contact_sheet as sheet_vendor
from omnipet._vendor.hatch.scripts import render_animation_previews as preview_vendor
from omnipet.hatch._runtime import hatch_operation, safe_output


@dataclass(frozen=True)
class InspectFramesConfig:
    frames_root: Path
    json_out: Path
    states: tuple[str, ...] = tuple(inspect_vendor.ROW_FRAME_COUNTS)
    min_used_pixels: int = 400
    edge_margin: int = 2
    edge_pixel_threshold: int = 24
    chroma_adjacent_threshold: float = 150.0
    chroma_adjacent_pixel_threshold: int = 800
    small_outlier_ratio: float = 0.35
    large_outlier_ratio: float = 2.75
    require_components: bool = False
    allow_stable_slots: bool = False


@dataclass(frozen=True)
class InspectionResult:
    ok: bool
    report: Path
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PreviewConfig:
    frames_root: Path
    output_dir: Path


@dataclass(frozen=True)
class PreviewResult:
    output_dir: Path
    previews: tuple[Path, ...]


@dataclass(frozen=True)
class ContactSheetConfig:
    atlas: Path
    output: Path
    scale: float = 0.5


@dataclass(frozen=True)
class ContactSheetResult:
    output: Path
    size: tuple[int, int]


def _path(value: object, name: str, *, exists: bool = False) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a path")
    path = value.expanduser().resolve()
    if exists and not path.exists():
        raise FileNotFoundError(path)
    return path


@hatch_operation
def inspect_frames(config: InspectFramesConfig) -> InspectionResult:
    root = _path(config.frames_root, "frames_root", exists=True)
    output = safe_output(config.json_out, "json_out")
    if not isinstance(config.states, tuple) or not config.states:
        raise TypeError("states must be a non-empty tuple")
    unknown = set(config.states) - set(inspect_vendor.ROW_FRAME_COUNTS)
    if unknown:
        raise ValueError(f"unknown states: {', '.join(sorted(unknown))}")
    args = SimpleNamespace(**{name: getattr(config, name) for name in (
        "min_used_pixels", "edge_margin", "edge_pixel_threshold", "chroma_adjacent_threshold",
        "chroma_adjacent_pixel_threshold", "small_outlier_ratio", "large_outlier_ratio",
        "require_components", "allow_stable_slots",
    )})
    manifest = inspect_vendor.load_manifest(root)
    chroma = inspect_vendor.load_chroma_key(root)
    rows = [inspect_vendor.inspect_state(root, state, inspect_vendor.ROW_FRAME_COUNTS[state], manifest, chroma, args) for state in config.states]
    errors = tuple(error for row in rows for error in row["errors"])
    warnings = tuple(warning for row in rows for warning in row["warnings"])
    payload = {"ok": not errors, "frames_root": str(root), "states": list(config.states), "errors": list(errors), "warnings": list(warnings), "rows": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return InspectionResult(not errors, output, errors, warnings)


@hatch_operation
def render_animation_previews(config: PreviewConfig) -> PreviewResult:
    root = _path(config.frames_root, "frames_root", exists=True)
    output_dir = safe_output(config.output_dir, "output_dir")
    outputs = []
    for state, durations in preview_vendor.ROW_DURATIONS.items():
        frames = preview_vendor.load_frames(root, state, len(durations))
        output = output_dir / f"{state}.gif"
        preview_vendor.save_preview(frames, durations, output)
        outputs.append(output)
    return PreviewResult(output_dir, tuple(outputs))


@hatch_operation
def make_contact_sheet(config: ContactSheetConfig) -> ContactSheetResult:
    atlas_path = _path(config.atlas, "atlas", exists=True)
    output = safe_output(config.output, "output")
    if config.scale <= 0:
        raise ValueError("scale must be positive")
    with Image.open(atlas_path) as opened:
        atlas = opened.convert("RGBA")
    rows = atlas.height // sheet_vendor.CELL_HEIGHT
    if atlas.width != sheet_vendor.COLUMNS * sheet_vendor.CELL_WIDTH or rows not in {9, 11}:
        raise ValueError(f"atlas must be 1536x1872 or 1536x2288; got {atlas.width}x{atlas.height}")
    cell_w, cell_h = max(1, round(192 * config.scale)), max(1, round(208 * config.scale))
    width, height = 8 * cell_w, rows * (cell_h + sheet_vendor.LABEL_HEIGHT)
    sheet = Image.new("RGB", (width, height), "#f7f7f7")
    draw, font = ImageDraw.Draw(sheet), ImageFont.load_default()
    for row in range(rows):
        y = row * (cell_h + sheet_vendor.LABEL_HEIGHT)
        draw.rectangle((0, y, width, y + sheet_vendor.LABEL_HEIGHT - 1), fill="#111111")
        draw.text((6, y + 5), f"row {row}: {sheet_vendor.ROW_NAMES[row]}", fill="#ffffff", font=font)
        draw.text((width - 92, y + 5), sheet_vendor.frame_count_label(rows, row), fill="#ffffff", font=font)
        for column in range(8):
            crop = atlas.crop((column * 192, row * 208, (column + 1) * 192, (row + 1) * 208)).resize((cell_w, cell_h), Image.Resampling.LANCZOS)
            background = sheet_vendor.checker((cell_w, cell_h))
            background.paste(crop, (0, 0), crop)
            x = column * cell_w
            sheet.paste(background, (x, y + sheet_vendor.LABEL_HEIGHT))
            draw.rectangle((x, y + sheet_vendor.LABEL_HEIGHT, x + cell_w - 1, y + sheet_vendor.LABEL_HEIGHT + cell_h - 1), outline="#18a058" if sheet_vendor.is_used_cell(rows, row, column) else "#cc3344")
            draw.text((x + 4, y + sheet_vendor.LABEL_HEIGHT + 4), str(column), fill="#111111", font=font)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return ContactSheetResult(output, sheet.size)
