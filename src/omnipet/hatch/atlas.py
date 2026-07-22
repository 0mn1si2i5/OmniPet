from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from omnipet._vendor.hatch.scripts import assemble_extended_atlas as extended_vendor
from omnipet._vendor.hatch.scripts import compose_atlas as compose_vendor
from omnipet._vendor.hatch.scripts import compose_cardinal_anchor_strip as cardinal_vendor
from omnipet._vendor.hatch.scripts import derive_running_left_from_running_right as mirror_vendor
from omnipet.hatch._runtime import hatch_operation, safe_output


@dataclass(frozen=True)
class ComposeAtlasConfig:
    output: Path
    frames_root: Path | None = None
    source_atlas: Path | None = None
    webp_output: Path | None = None
    resize_source: bool = False


@dataclass(frozen=True)
class ComposeAtlasResult:
    output: Path
    webp_output: Path | None
    size: tuple[int, int]


@dataclass(frozen=True)
class AssembleExtendedAtlasConfig:
    base_atlas: Path
    output: Path | None = None
    look_cells_dir: Path | None = None
    look_row_9: Path | None = None
    look_row_10: Path | None = None
    registered_row_9: Path | None = None
    row_9_registration: Path | None = None
    neutral_cell: Path | None = None
    registered_row_output: Path | None = None
    registration_manifest_output: Path | None = None
    webp_output: Path | None = None
    manifest_output: Path | None = None
    chroma_key: str = "#00FF00"
    chroma_threshold: float = 96.0
    edge_margin: int = 2
    edge_pixel_threshold: int = 24


@dataclass(frozen=True)
class AssembleExtendedAtlasResult:
    output: Path
    webp_output: Path | None
    manifest: Path | None
    size: tuple[int, int]


@dataclass(frozen=True)
class CardinalStripResult:
    output: Path
    size: tuple[int, int]


@dataclass(frozen=True)
class DeriveRunningLeftConfig:
    run_dir: Path
    approved: bool
    decision_note: str
    force: bool = False


@dataclass(frozen=True)
class DeriveRunningLeftResult:
    output: Path
    manifest: Path
    transform: str


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
def compose_atlas(config: ComposeAtlasConfig) -> ComposeAtlasResult:
    output = safe_output(config.output, "output")
    if (config.frames_root is None) == (config.source_atlas is None):
        raise ValueError("provide exactly one of frames_root or source_atlas")
    if config.frames_root is not None:
        atlas = compose_vendor.compose_from_frames(_path(config.frames_root, "frames_root", directory=True))
    else:
        atlas = compose_vendor.compose_from_source_atlas(_path(config.source_atlas, "source_atlas", file=True), config.resize_source)
    webp = safe_output(config.webp_output, "webp_output") if config.webp_output is not None else None
    compose_vendor.save_outputs(atlas, output, webp)
    return ComposeAtlasResult(output, webp, atlas.size)


@hatch_operation
def compose_cardinal_anchor_strip(*, anchors_dir: Path, output: Path) -> CardinalStripResult:
    root = _path(anchors_dir, "anchors_dir", directory=True)
    destination = safe_output(output, "output")
    strip = Image.new("RGBA", (cardinal_vendor.CELL_SIZE[0] * 4, cardinal_vendor.CELL_SIZE[1]))
    for index, direction in enumerate(cardinal_vendor.CARDINALS):
        path = root / f"{direction}.png"
        if not path.is_file():
            raise FileNotFoundError(path)
        with Image.open(path) as opened:
            reference = opened.convert("RGBA")
        if reference.size != cardinal_vendor.CELL_SIZE or reference.getbbox() is None:
            raise ValueError(f"invalid cardinal reference: {path}")
        strip.alpha_composite(reference, (index * cardinal_vendor.CELL_SIZE[0], 0))
    destination.parent.mkdir(parents=True, exist_ok=True)
    strip.save(destination)
    return CardinalStripResult(destination, strip.size)


@hatch_operation
def assemble_extended_atlas(config: AssembleExtendedAtlasConfig) -> AssembleExtendedAtlasResult:
    base = _path(config.base_atlas, "base_atlas", file=True)
    sources = [config.look_cells_dir is not None, config.look_row_9 is not None, config.registered_row_9 is not None]
    if sum(sources) != 1:
        raise ValueError("provide exactly one look source")
    try:
        chroma = extended_vendor.parse_hex_color(config.chroma_key)
    except SystemExit as exc:
        raise ValueError(str(exc)) from None
    atlas = extended_vendor.load_base_rows(base)
    neutral = extended_vendor.load_neutral_cell(
        _path(config.neutral_cell, "neutral_cell", file=True) if config.neutral_cell else None,
        atlas, chroma, config.chroma_threshold,
    )
    if config.registered_row_9 is not None:
        if config.look_row_10 is None or config.row_9_registration is None:
            raise ValueError("registered_row_9 requires look_row_10 and row_9_registration")
        first = extended_vendor.load_registered_row(_path(config.registered_row_9, "registered_row_9", file=True))
        second = extended_vendor.extract_row_strip_cells(_path(config.look_row_10, "look_row_10", file=True), chroma, config.chroma_threshold)
        second = extended_vendor.normalize_cells_to_reference(second, neutral, extended_vendor.load_registration_scale(_path(config.row_9_registration, "row_9_registration", file=True)))
        extended_vendor.validate_normalized_look_cells(second, 8, config.edge_margin, config.edge_pixel_threshold)
        cells, scale = [*first, *second], None
    else:
        if config.look_cells_dir is not None:
            cells = extended_vendor.load_look_cells_from_dir(_path(config.look_cells_dir, "look_cells_dir", directory=True), chroma, config.chroma_threshold)
        else:
            first = extended_vendor.extract_row_strip_cells(_path(config.look_row_9, "look_row_9", file=True), chroma, config.chroma_threshold)
            cells = first if config.look_row_10 is None else [*first, *extended_vendor.extract_row_strip_cells(_path(config.look_row_10, "look_row_10", file=True), chroma, config.chroma_threshold)]
        target = extended_vendor.cell_geometry(neutral)
        if target is None:
            raise ValueError("neutral reference cell must contain visible pixels")
        scale = extended_vendor.normalization_scale(cells, target)
        cells = extended_vendor.normalize_cells_to_reference(cells, neutral, scale)
        extended_vendor.validate_normalized_look_cells(cells, 0, config.edge_margin, config.edge_pixel_threshold)
    if len(cells) == 8:
        if config.registered_row_output is None or config.registration_manifest_output is None:
            raise ValueError("row 9 registration outputs are required")
        row_output = safe_output(config.registered_row_output, "registered_row_output")
        registration = safe_output(config.registration_manifest_output, "registration_manifest_output")
        extended_vendor.save_registered_row(cells, row_output)
        extended_vendor.write_registration_manifest(registration, scale)
        registration.write_text(json.dumps({
            "ok": True,
            "scale": scale,
            "source_sha256": _sha256(_path(config.look_row_9, "look_row_9", file=True)),
            "registered_sha256": _sha256(row_output),
        }, indent=2) + "\n", encoding="utf-8")
        return AssembleExtendedAtlasResult(row_output, None, registration, (1536, 208))
    if config.output is None:
        raise ValueError("output is required for extended atlas assembly")
    output = safe_output(config.output, "output")
    extended_vendor.paste_look_cells(atlas, cells)
    extended_vendor.paste_neutral_cell(atlas, neutral)
    atlas = extended_vendor.clear_transparent_rgb(atlas)
    output.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(output)
    webp = safe_output(config.webp_output, "webp_output") if config.webp_output else None
    if webp:
        webp.parent.mkdir(parents=True, exist_ok=True)
        atlas.save(webp, format="WEBP", lossless=True, quality=100, method=6, exact=True)
    manifest = safe_output(config.manifest_output, "manifest_output") if config.manifest_output else None
    if manifest:
        extended_vendor.write_manifest(manifest, webp or output)
    return AssembleExtendedAtlasResult(output, webp, manifest, atlas.size)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@hatch_operation
def derive_running_left(config: DeriveRunningLeftConfig) -> DeriveRunningLeftResult:
    run_dir = _path(config.run_dir, "run_dir", directory=True)
    if not config.approved:
        raise ValueError("mirroring requires explicit approval")
    if not isinstance(config.decision_note, str) or not config.decision_note.strip():
        raise ValueError("decision_note must explain why mirroring is appropriate")
    manifest_path = run_dir / "imagegen-jobs.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = mirror_vendor.load_manifest(run_dir)
    right = mirror_vendor.find_job(manifest, "running-right")
    left = mirror_vendor.find_job(manifest, "running-left")
    if right.get("status") != "complete":
        raise ValueError("running-right must be complete")
    policy = left.get("mirror_policy")
    if not isinstance(policy, dict) or policy.get("may_derive_from") != "running-right":
        raise ValueError("running-left is not configured for mirroring")
    source, output = run_dir / "decoded" / "running-right.png", run_dir / "decoded" / "running-left.png"
    if not source.is_file():
        raise FileNotFoundError(source)
    if output.exists() and not config.force:
        raise FileExistsError(output)
    with Image.open(source) as opened:
        mirrored = mirror_vendor.mirror_strip_preserving_frame_order(opened)
    output.parent.mkdir(parents=True, exist_ok=True)
    mirrored.save(output)
    completed = datetime.now(timezone.utc).isoformat()
    left.update({"status": "complete", "source_path": mirror_vendor.manifest_relative(source, run_dir), "derived_from": "running-right", "completed_at": completed, "metadata": mirror_vendor.image_metadata(output), "mirror_decision": {"approved": True, "approved_at": completed, "note": config.decision_note.strip(), "transform": "framewise-horizontal-mirror-preserving-order"}})
    for key in ("last_error", "repair_reason", "queued_at"):
        left.pop(key, None)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return DeriveRunningLeftResult(output, manifest_path, "framewise-horizontal-mirror-preserving-order")
