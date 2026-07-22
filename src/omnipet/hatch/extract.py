from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image

from omnipet._vendor.hatch.scripts import extract_cardinal_anchors as cardinal_vendor
from omnipet._vendor.hatch.scripts import extract_strip_frames as strip_vendor
from omnipet.hatch._runtime import hatch_operation, safe_output

ExtractionMethod = Literal["auto", "components", "slots", "stable-slots"]


@dataclass(frozen=True)
class ExtractStripFramesConfig:
    decoded_dir: Path
    output_dir: Path
    states: tuple[str, ...] = tuple(strip_vendor.ROW_FRAME_COUNTS)
    chroma_key: str | None = None
    key_threshold: float = 96.0
    method: ExtractionMethod = "auto"


@dataclass(frozen=True)
class ExtractStripFramesResult:
    frames_root: Path
    manifest: Path
    states: tuple[str, ...]


@dataclass(frozen=True)
class CardinalAnchorsConfig:
    strip: Path
    output_dir: Path
    json_out: Path
    chroma_key: str
    chroma_threshold: float = 96.0
    edge_margin: int = 2
    edge_pixel_threshold: int = 24
    min_used_pixels: int = 400


@dataclass(frozen=True)
class CardinalAnchorsResult:
    ok: bool
    report: Path
    anchors: tuple[Path, ...]
    errors: tuple[str, ...]


def _path(value: object, name: str, *, file: bool = False, directory: bool = False) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a path")
    if file and value.expanduser().absolute().is_symlink():
        raise ValueError(f"{name} must be a regular file")
    path = value.expanduser().resolve()
    if file and not path.is_file():
        raise FileNotFoundError(path)
    if directory and not path.is_dir():
        raise FileNotFoundError(path)
    return path


@hatch_operation
def extract_strip_frames(config: ExtractStripFramesConfig) -> ExtractStripFramesResult:
    if not isinstance(config, ExtractStripFramesConfig):
        raise TypeError("config must be ExtractStripFramesConfig")
    decoded = _path(config.decoded_dir, "decoded_dir", directory=True)
    output = safe_output(config.output_dir, "output_dir")
    if not isinstance(config.states, tuple) or not config.states:
        raise TypeError("states must be a non-empty tuple")
    unknown = sorted(set(config.states) - set(strip_vendor.ROW_FRAME_COUNTS))
    if unknown:
        raise ValueError(f"unknown states: {', '.join(unknown)}")
    if config.method not in {"auto", "components", "slots", "stable-slots"}:
        raise ValueError(f"unknown extraction method: {config.method}")
    if config.key_threshold < 0:
        raise ValueError("key_threshold must not be negative")
    chroma = strip_vendor.load_chroma_key(decoded, config.chroma_key)
    rows = []
    for state in config.states:
        source = decoded / f"{state}.png"
        if not source.is_file():
            raise FileNotFoundError(source)
        rows.append(strip_vendor.extract_state(source, state, output, chroma, config.key_threshold, config.method))
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "frames-manifest.json"
    manifest.write_text(json.dumps({
        "ok": True,
        "chroma_key": {"hex": f"#{chroma[0]:02X}{chroma[1]:02X}{chroma[2]:02X}", "rgb": list(chroma), "threshold": config.key_threshold},
        "rows": rows,
    }, indent=2) + "\n", encoding="utf-8")
    return ExtractStripFramesResult(output, manifest, config.states)


@hatch_operation
def extract_cardinal_anchors(config: CardinalAnchorsConfig) -> CardinalAnchorsResult:
    if not isinstance(config, CardinalAnchorsConfig):
        raise TypeError("config must be CardinalAnchorsConfig")
    strip_path = _path(config.strip, "strip", file=True)
    output = safe_output(config.output_dir, "output_dir")
    report = safe_output(config.json_out, "json_out")
    try:
        chroma = cardinal_vendor.parse_hex_color(config.chroma_key)
    except SystemExit as exc:
        raise ValueError(str(exc)) from None
    if config.chroma_threshold < 0 or config.edge_margin < 1 or config.edge_pixel_threshold < 0 or config.min_used_pixels < 0:
        raise ValueError("cardinal thresholds are out of range")
    output.mkdir(parents=True, exist_ok=True)
    with Image.open(strip_path) as opened:
        strip = cardinal_vendor.remove_chroma_background(opened, chroma, config.chroma_threshold)
    slot_width = strip.width / len(cardinal_vendor.CARDINALS)
    anchors = []
    records = []
    errors = []
    for index, label in enumerate(cardinal_vendor.CARDINALS):
        left, right = round(index * slot_width), round((index + 1) * slot_width)
        cell = strip.crop((left, 0, right, strip.height))
        used = cardinal_vendor.alpha_count(cell)
        edge = cardinal_vendor.edge_alpha_count(cell, config.edge_margin)
        path = output / f"{label}.png"
        cardinal_vendor.fit_to_cell(cell).save(path)
        if used < config.min_used_pixels:
            errors.append(f"{label} is empty or too sparse ({used} pixels)")
        if edge > config.edge_pixel_threshold:
            errors.append(f"{label} has {edge} non-transparent pixels near its source slot edge")
        anchors.append(path)
        records.append({"direction": label, "source_box": [left, 0, right, strip.height], "used_pixels": used, "edge_pixels": edge, "output": str(path)})
    payload = {"ok": not errors, "strip": str(strip_path), "directions": cardinal_vendor.CARDINALS, "errors": errors, "anchors": records}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return CardinalAnchorsResult(not errors, report, tuple(anchors), tuple(errors))
