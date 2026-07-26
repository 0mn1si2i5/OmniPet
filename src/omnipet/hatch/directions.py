from __future__ import annotations

import hashlib
import json
import random
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from omnipet._vendor.hatch.scripts import make_direction_blind_qa_sheet as blind_vendor
from omnipet._vendor.hatch.scripts import make_direction_qa_sheet as qa_vendor
from omnipet._vendor.hatch.scripts import measure_direction_continuity as continuity_vendor
from omnipet._vendor.hatch.scripts import validate_direction_blind_verdicts as verdict_vendor
from omnipet.hatch._runtime import hatch_operation, safe_output


@dataclass(frozen=True)
class DirectionQaSheetConfig:
    atlas: Path
    output: Path


@dataclass(frozen=True)
class BlindQaSheetConfig:
    atlas: Path
    output: Path
    answer_key: Path


@dataclass(frozen=True)
class ContinuityConfig:
    atlas: Path
    json_out: Path
    diff_outlier_ratio: float = 1.45
    center_delta_warning: float = 8.0
    area_ratio_warning: float = 1.15


@dataclass(frozen=True)
class CombineVerdictsConfig:
    verdicts: tuple[Path, ...]
    json_out: Path


@dataclass(frozen=True)
class BlindVerdictConfig:
    answer_key: Path
    verdicts: Path
    json_out: Path


@dataclass(frozen=True)
class SheetResult:
    output: Path


@dataclass(frozen=True)
class BlindSheetResult:
    output: Path
    answer_key_path: Path
    answer_key: dict[str, Any]


@dataclass(frozen=True)
class ReportResult:
    report_path: Path
    report: dict[str, Any]


@dataclass(frozen=True)
class VerdictResult:
    ok: bool
    report_path: Path
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


def _atlas(path: Path) -> Image.Image:
    source = _path(path, "atlas", file=True)
    with Image.open(source) as opened:
        atlas = opened.convert("RGBA")
    if atlas.size != (1536, 2288):
        raise ValueError(f"extended atlas must be 1536x2288; got {atlas.width}x{atlas.height}")
    return atlas


@hatch_operation
def make_direction_qa_sheet(config: DirectionQaSheetConfig) -> SheetResult:
    atlas = _atlas(config.atlas)
    output = safe_output(config.output, "output")
    sheet = Image.new("RGBA", (8 * 192, 5 * (208 + qa_vendor.LABEL_HEIGHT)), (255, 255, 255, 255))
    qa_vendor.paste_labeled_cell(sheet, atlas, label="neutral", row_index=0, column_index=6, output_column=0, output_row=0)
    for index, (label, direction) in enumerate(qa_vendor.LOOK_DIRECTION_LABELS):
        qa_vendor.paste_labeled_cell(sheet, atlas, label=f"{label} {direction}", row_index=9 + index // 8, column_index=index % 8, output_column=index % 8, output_row=1 + index // 8)
        qa_vendor.paste_labeled_focus_cell(sheet, atlas, label=f"zoom {label} {direction}", row_index=9 + index // 8, column_index=index % 8, output_column=index % 8, output_row=3 + index // 8)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(output)
    return SheetResult(output)


@hatch_operation
def make_direction_blind_qa_sheet(config: BlindQaSheetConfig) -> BlindSheetResult:
    atlas_path = _path(config.atlas, "atlas", file=True)
    atlas = _atlas(atlas_path)
    output, key_path = safe_output(config.output, "output"), safe_output(config.answer_key, "answer_key")
    rng = random.Random(int.from_bytes(hashlib.sha256(atlas.tobytes()).digest()[:8], "big"))
    sheet = Image.new("RGBA", (2 * 192, len(blind_vendor.AXIS_PAIRS) * (208 + blind_vendor.LABEL_HEIGHT)), (255, 255, 255, 255))
    answers = []
    indexes = {"horizontal": 0, "vertical": 0}
    for row, (axis, first_label, first_direction, second_label, second_direction) in enumerate(blind_vendor.AXIS_PAIRS):
        indexes[axis] += 1
        pair = [(first_label, first_direction), (second_label, second_direction)]
        rng.shuffle(pair)
        blind_vendor.paste_cell(sheet, blind_vendor.atlas_cell(atlas, pair[0][0]), label=f"{axis.title()} pair {indexes[axis]} A", column=0, row=row)
        blind_vendor.paste_cell(sheet, blind_vendor.atlas_cell(atlas, pair[1][0]), label=f"{axis.title()} pair {indexes[axis]} B", column=1, row=row)
        answers.append({"pair": f"{axis}-{indexes[axis]}", "axis": axis, "gate": "hard" if {first_label, second_label} in ({"000", "180"}, {"090", "270"}) else "review", "A": {"expected_direction": pair[0][1], "source_direction": pair[0][0]}, "B": {"expected_direction": pair[1][1], "source_direction": pair[1][0]}})
    payload = {"schema_version": 3, "atlas_sha256": hashlib.sha256(atlas_path.read_bytes()).hexdigest(), "instructions": "Do not provide this answer key to the blind visual QA reviewer.", "pairs": answers}
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(output)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return BlindSheetResult(output, key_path, payload)


@hatch_operation
def measure_direction_continuity(config: ContinuityConfig) -> ReportResult:
    if config.diff_outlier_ratio <= 0 or config.center_delta_warning < 0 or config.area_ratio_warning < 1:
        raise ValueError("continuity thresholds are out of range")
    atlas_path = _path(config.atlas, "atlas", file=True)
    atlas = _atlas(atlas_path)
    cells = [continuity_vendor.cell_from_atlas(atlas, index) for index in range(16)]
    pairs = [{"from": label, "to": continuity_vendor.LOOK_DIRECTION_LABELS[(index + 1) % 16], **continuity_vendor.pair_metric(cells[index], cells[(index + 1) % 16])} for index, label in enumerate(continuity_vendor.LOOK_DIRECTION_LABELS)]
    diffs = [float(pair["diffPixels"]) for pair in pairs]
    warnings, holes = [], []
    for label, cell in zip(continuity_vendor.LOOK_DIRECTION_LABELS, cells, strict=True):
        found = continuity_vendor.transparent_hole_rows(cell)
        if found:
            holes.append({"direction": label, "holes": found})
            warnings.append({
                "id": f"direction-continuity:direction-{label}:transparent-interior-hole-rows",
                "text": f"{label} has transparent interior hole rows",
            })
    for index, pair in enumerate(pairs):
        label = f"{pair['from']}->{pair['to']}"
        neighbors = statistics.mean([diffs[(index - 1) % 16], diffs[(index + 1) % 16]])
        if neighbors and float(pair["diffPixels"]) > neighbors * config.diff_outlier_ratio:
            warnings.append({
                "id": f"direction-continuity:pair-{pair['from']}-to-{pair['to']}:diff-local-outlier",
                "text": f"{label} diff is a local outlier",
            })
        if isinstance(pair["centerDelta"], float) and pair["centerDelta"] > config.center_delta_warning:
            warnings.append({
                "id": f"direction-continuity:pair-{pair['from']}-to-{pair['to']}:center-shift-high",
                "text": f"{label} center shift is high",
            })
        if isinstance(pair["areaRatio"], float) and pair["areaRatio"] > config.area_ratio_warning:
            warnings.append({
                "id": f"direction-continuity:pair-{pair['from']}-to-{pair['to']}:sprite-area-ratio-high",
                "text": f"{label} sprite area ratio is high",
            })
    payload = {
        "ok": True,
        "atlasSha256": hashlib.sha256(atlas_path.read_bytes()).hexdigest(),
        "reviewRequired": bool(warnings),
        "medianDiffPixels": continuity_vendor.median(diffs),
        "warnings": warnings,
        "alphaHoles": holes,
        "pairs": pairs,
    }
    output = safe_output(config.json_out, "json_out")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return ReportResult(output, payload)


@hatch_operation
def combine_verdicts(config: CombineVerdictsConfig) -> ReportResult:
    if not isinstance(config.verdicts, tuple):
        raise TypeError("verdicts must be a tuple of paths")
    if len(config.verdicts) < 3 or len(config.verdicts) % 2 == 0:
        raise ValueError("provide an odd number of at least three verdict files")
    reviews = []
    for path in config.verdicts:
        payload = json.loads(_path(path, "verdict", file=True).read_text(encoding="utf-8"))
        reviews.append({entry["pair"]: entry for entry in payload.get("pairs", [])})
    ids = set(reviews[0])
    if any(set(review) != ids for review in reviews[1:]):
        raise ValueError("all verdict files must contain the same pair ids")
    threshold, combined = len(reviews) // 2 + 1, []
    for pair_id in reviews[0]:
        result: dict[str, Any] = {"pair": pair_id}
        votes = {}
        for slot in ("A", "B"):
            counts = Counter(review[pair_id].get(slot) for review in reviews)
            direction, count = counts.most_common(1)[0]
            result[slot], votes[slot] = direction if count >= threshold else "ambiguous", dict(counts)
        result.update({"reason": "strict majority of independent blind reviews", "votes": votes})
        combined.append(result)
    payload = {"pairs": combined}
    output = safe_output(config.json_out, "json_out")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return ReportResult(output, payload)


@hatch_operation
def validate_blind_verdicts(config: BlindVerdictConfig) -> VerdictResult:
    answer = json.loads(_path(config.answer_key, "answer_key", file=True).read_text(encoding="utf-8"))
    verdicts = json.loads(_path(config.verdicts, "verdicts", file=True).read_text(encoding="utf-8"))
    expected_by_pair = {entry["pair"]: entry for entry in answer.get("pairs", [])}
    observed_by_pair = {entry["pair"]: entry for entry in verdicts.get("pairs", [])}
    errors, warnings, unconfirmed, results = [], [], [], []
    for pair_id, expected in expected_by_pair.items():
        observed = observed_by_pair.get(pair_id)
        if observed is None:
            errors.append(f"missing blind verdict for {pair_id}")
            continue
        axis, gate = expected.get("axis", "horizontal"), expected.get("gate", "hard")
        if gate not in {"hard", "review"}:
            raise ValueError(f"{pair_id} has invalid gate: {gate!r}")
        result: dict[str, Any] = {"pair": pair_id, "axis": axis, "gate": gate}
        for slot in ("A", "B"):
            value, wanted = observed.get(slot), expected[slot].get("expected_direction", expected[slot].get("expected_horizontal"))
            if value not in verdict_vendor.ALLOWED_DIRECTIONS:
                errors.append(f"{pair_id} {slot} has invalid classification: {value!r}")
            elif value == "ambiguous":
                message = f"{pair_id} {slot} {axis} axis is ambiguous"
                warnings.append(message)
                if gate == "hard": unconfirmed.append(message)
            elif value != wanted:
                (errors if gate == "hard" else warnings).append(f"{pair_id} {slot} classified {value}; expected {wanted}")
            result[slot] = {"observed": value, "expected": wanted, "source_direction": expected[slot]["source_direction"], "pass": value == wanted}
        if observed.get("A") == observed.get("B") and observed.get("A") != "ambiguous":
            (errors if gate == "hard" else warnings).append(f"{pair_id} A and B were classified as the same {axis} direction")
        results.append(result)
    extra = sorted(set(observed_by_pair) - set(expected_by_pair))
    if extra: errors.append(f"unexpected blind verdict pairs: {', '.join(extra)}")
    payload = {"ok": not errors and not unconfirmed, "errors": errors, "warnings": warnings, "unconfirmed": unconfirmed, "reviewRequired": bool(warnings), "pairs": results}
    output = safe_output(config.json_out, "json_out")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return VerdictResult(payload["ok"], output, payload)
