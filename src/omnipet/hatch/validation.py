from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from omnipet._vendor.hatch.scripts import validate_atlas as vendor
from omnipet.hatch._runtime import hatch_operation, safe_output


@dataclass(frozen=True)
class ValidateAtlasConfig:
    atlas: Path
    json_out: Path | None = None
    min_used_pixels: int = 50
    near_opaque_threshold: float = 0.95
    chroma_key: str = "#00FF00"
    chroma_leak_threshold: float = 36.0
    max_chroma_leak_pixels: int = 400
    chroma_fringe_threshold: float = 96.0
    chroma_fringe_edge_radius: int = 2
    chroma_fringe_alpha_minimum: int = 16
    max_chroma_fringe_pixels: int = 0
    allow_opaque: bool = False
    allow_near_opaque_used_cells: bool = False
    allow_chroma_leak: bool = False
    allow_chroma_fringe: bool = False
    require_v2: bool = False


@dataclass(frozen=True)
class ValidateAtlasResult:
    ok: bool
    report_path: Path | None
    report: dict[str, Any]


def _path(value: object, name: str, *, file: bool = False) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a path")
    if file and value.expanduser().absolute().is_symlink():
        raise ValueError(f"{name} must be a regular file")
    path = value.expanduser().resolve()
    if file and not path.is_file():
        raise FileNotFoundError(path)
    return path


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is out of range")
    return value


def _number(value: object, name: str, *, minimum: float = 0, maximum: float | None = None) -> float:
    if type(value) not in {int, float}:
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum or maximum is not None and number > maximum:
        raise ValueError(f"{name} is out of range")
    return number


@hatch_operation
def validate_atlas(config: ValidateAtlasConfig) -> ValidateAtlasResult:
    path = _path(config.atlas, "atlas", file=True)
    _integer(config.min_used_pixels, "min_used_pixels", minimum=1)
    _number(config.near_opaque_threshold, "near_opaque_threshold", maximum=1)
    _number(config.chroma_leak_threshold, "chroma_leak_threshold")
    _integer(config.max_chroma_leak_pixels, "max_chroma_leak_pixels")
    _number(config.chroma_fringe_threshold, "chroma_fringe_threshold")
    _integer(config.chroma_fringe_edge_radius, "chroma_fringe_edge_radius")
    _integer(config.chroma_fringe_alpha_minimum, "chroma_fringe_alpha_minimum", maximum=255)
    _integer(config.max_chroma_fringe_pixels, "max_chroma_fringe_pixels")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", config.chroma_key):
        raise ValueError("chroma_key must be #RRGGBB")
    chroma = tuple(int(config.chroma_key[index:index + 2], 16) for index in (1, 3, 5))
    with Image.open(path) as opened:
        source_mode, source_format = opened.mode, opened.format
        image = opened.convert("RGBA")
    errors: list[str] = []
    warnings: list[str] = []
    expected_heights = {vendor.EXTENDED_ATLAS_HEIGHT} if config.require_v2 else {vendor.ATLAS_HEIGHT, vendor.EXTENDED_ATLAS_HEIGHT}
    if image.width != vendor.ATLAS_WIDTH or image.height not in expected_heights:
        errors.append(f"expected valid atlas dimensions, got {image.width}x{image.height}")
    if source_format not in {"PNG", "WEBP"}:
        errors.append(f"expected PNG or WebP, got {source_format}")
    if "A" not in source_mode and not config.allow_opaque:
        errors.append("atlas does not have an alpha channel")
    row_count, extended = image.height // vendor.CELL_HEIGHT, image.height == vendor.EXTENDED_ATLAS_HEIGHT
    cells: list[dict[str, Any]] = []
    near_opaque: dict[str, list[int]] = defaultdict(list)
    for row in range(min(row_count, vendor.EXTENDED_ROWS)):
        state, count = vendor.ROW_BY_INDEX[row]
        for column in range(vendor.COLUMNS):
            cell = image.crop((column * 192, row * 208, (column + 1) * 192, (row + 1) * 208))
            pixels = vendor.alpha_nonzero_count(cell)
            used = column < count or (extended and (row, column) == vendor.EXTENDED_NEUTRAL_LOOK_FRAME)
            leak = vendor.opaque_chroma_key_count(cell, chroma, config.chroma_leak_threshold)
            fringe = vendor.chroma_fringe_count(cell, chroma_key=chroma, distance_threshold=config.chroma_fringe_threshold, edge_radius=config.chroma_fringe_edge_radius, alpha_minimum=config.chroma_fringe_alpha_minimum)
            cells.append({"state": state, "row": row, "column": column, "used": used, "nontransparent_pixels": pixels, "opaque_chroma_key_pixels": leak, "chroma_fringe_pixels": fringe})
            if used and pixels < config.min_used_pixels:
                errors.append(f"{state} row {row} column {column} is empty or too sparse ({pixels} pixels)")
            if used and leak > config.max_chroma_leak_pixels:
                (warnings if config.allow_chroma_leak else errors).append(f"{state} row {row} column {column} has {leak} opaque chroma pixels")
            if used and fringe > config.max_chroma_fringe_pixels:
                (warnings if config.allow_chroma_fringe else errors).append(f"{state} row {row} column {column} has {fringe} chroma fringe pixels")
            if used and pixels > 192 * 208 * config.near_opaque_threshold:
                near_opaque[f"{state} row {row}"].append(column)
            if not used and pixels:
                errors.append(f"{state} row {row} unused column {column} is not transparent ({pixels} pixels)")
    for label, columns in near_opaque.items():
        (warnings if config.allow_near_opaque_used_cells else errors).append(f"{label} has {len(columns)} nearly opaque used cells")
    alpha_count = vendor.alpha_nonzero_count(image)
    if alpha_count == image.width * image.height:
        (warnings if config.allow_opaque else errors).append("atlas is fully opaque")
    residue = vendor.transparent_rgb_residue_count(image)
    if residue:
        errors.append(f"atlas has {residue} fully transparent pixels with non-zero RGB residue")
    report = {"ok": not errors, "file": str(path), "format": source_format, "mode": source_mode, "columns": 8, "rows": row_count, "sprite_version_number": 2 if extended else 1, "width": image.width, "height": image.height, "transparent_rgb_residue_pixels": residue, "errors": errors, "warnings": warnings, "cells": cells}
    report_path = safe_output(config.json_out, "json_out") if config.json_out else None
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return ValidateAtlasResult(not errors, report_path, report)
