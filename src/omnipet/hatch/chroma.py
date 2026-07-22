from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from omnipet._vendor.hatch.scripts import despill_chroma_edges as vendor
from omnipet.hatch._runtime import hatch_operation, safe_output


@dataclass(frozen=True)
class DespillConfig:
    input: Path
    output: Path
    chroma_key: str
    webp_output: Path | None = None
    json_out: Path | None = None
    strength: float = 1.0
    edge_radius: int = 5
    spill_tolerance: float = 0.15
    minimum_saturation: float = 0.1


@dataclass(frozen=True)
class DespillResult:
    output: Path
    webp_output: Path | None
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


@hatch_operation
def despill(config: DespillConfig) -> DespillResult:
    source = _path(config.input, "input", file=True)
    output = safe_output(config.output, "output")
    if not isinstance(config.chroma_key, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", config.chroma_key):
        raise ValueError("chroma_key must be #RRGGBB")
    chroma = tuple(int(config.chroma_key[index:index + 2], 16) for index in (1, 3, 5))
    with Image.open(source) as opened:
        cleaned, details = vendor.decontaminate_image(
            opened, chroma_key=chroma, strength=config.strength, edge_radius=config.edge_radius,
            spill_tolerance=config.spill_tolerance, minimum_saturation=config.minimum_saturation,
        )
    vendor.save_image(cleaned, output)
    webp = safe_output(config.webp_output, "webp_output") if config.webp_output else None
    if webp:
        vendor.save_image(cleaned, webp)
    report = {"ok": True, "input": str(source), "output": str(output), "chroma_key": config.chroma_key.upper(), **details}
    report_path = safe_output(config.json_out, "json_out") if config.json_out else None
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return DespillResult(output, webp, report_path, report)
