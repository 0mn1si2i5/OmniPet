from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from omnipet._vendor.hatch.scripts import prepare_pet_run as vendor
from omnipet.hatch._runtime import hatch_operation, safe_output


STYLE_PRESETS = frozenset(vendor.STYLE_PRESETS)


@dataclass(frozen=True)
class PrepareRunInputs:
    pet_id: str
    display_name: str
    description: str
    style_preset: str
    style_notes: str
    output_dir: Path
    references: tuple[Path, ...] = ()


@dataclass(frozen=True)
class PreparedRun:
    run_dir: Path
    request: Path
    jobs: Path


@hatch_operation
def prepare_run(inputs: PrepareRunInputs) -> PreparedRun:
    if not isinstance(inputs, PrepareRunInputs):
        raise TypeError("inputs must be PrepareRunInputs")
    if not isinstance(inputs.references, tuple) or not all(
        isinstance(reference, Path) for reference in inputs.references
    ):
        raise TypeError("references must be a tuple of paths")
    if not all(
        isinstance(value, str)
        for value in (
            inputs.pet_id,
            inputs.display_name,
            inputs.description,
            inputs.style_preset,
            inputs.style_notes,
        )
    ):
        raise TypeError("text inputs must be strings")
    if not isinstance(inputs.output_dir, Path):
        raise TypeError("output_dir must be a path")

    run_dir = safe_output(inputs.output_dir, "output_dir")
    references = tuple(reference.expanduser().resolve() for reference in inputs.references)
    style_preset = inputs.style_preset.strip().lower()
    style_notes = inputs.style_notes.strip()
    if style_preset not in STYLE_PRESETS:
        style_notes = f"Requested style: {style_preset}. {style_notes}".strip()
        style_preset = "auto"
    args = argparse.Namespace(
        pet_id=vendor.slugify(inputs.pet_id),
        display_name=inputs.display_name.strip(),
        pet_name=inputs.display_name.strip(),
        description=inputs.description.strip(),
        pet_notes=inputs.description.strip().rstrip("."),
        style_preset=style_preset,
        style_notes=style_notes,
        style_contract=vendor.resolved_style_contract(style_preset, style_notes),
        brand_name="",
        brand_brief="",
        brand_source=[],
        chroma_key="auto",
    )
    if not args.pet_id or not args.display_name or not args.description:
        raise ValueError("pet identity fields must not be blank")
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError("run directory is not empty")
    for reference in references:
        if reference.is_symlink() or not reference.is_file():
            raise ValueError("reference is not a regular file")

    run_dir.mkdir(parents=True, exist_ok=True)
    reference_dir = run_dir / "references"
    prompt_dir = run_dir / "prompts"
    row_prompt_dir = prompt_dir / "rows"
    row_retry_dir = prompt_dir / "row-retries"
    repair_prompt_dir = prompt_dir / "look-anchor-repairs"
    for directory in (
        reference_dir,
        prompt_dir,
        row_prompt_dir,
        row_retry_dir,
        repair_prompt_dir,
        run_dir / "decoded",
        run_dir / "qa",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    copied_refs: list[dict[str, object]] = []
    copied_paths: list[Path] = []
    for index, source in enumerate(references, start=1):
        suffix = source.suffix.lower() or ".png"
        copied = reference_dir / f"reference-{index:02d}{suffix}"
        shutil.copy2(source, copied)
        metadata = vendor.image_metadata(copied)
        metadata["source_path"] = str(source)
        metadata["copied_path"] = str(copied)
        copied_refs.append(metadata)
        copied_paths.append(copied)

    args.chroma_key = vendor.choose_chroma_key(copied_paths, "auto")
    guides = vendor.create_layout_guides(run_dir)
    created_at = datetime.now(timezone.utc).isoformat()
    request = {
        "pet_id": args.pet_id,
        "display_name": args.display_name,
        "description": args.description,
        "created_at": created_at,
        "sprite_version_number": 2,
        "atlas": vendor.ATLAS,
        "rows": [
            {"state": state, "row": row, "frames": frames, "purpose": purpose}
            for state, row, frames, purpose in vendor.ROWS
        ] + [
            {
                "state": state,
                "row": row,
                "frames": len(directions),
                "directions": directions,
                "purpose": purpose,
            }
            for state, row, directions, purpose in vendor.LOOK_ROWS
        ],
        "layout_guides": [
            {**guide, "path": vendor.rel(Path(str(guide["path"])), run_dir)}
            for guide in guides
        ],
        "references": copied_refs,
        "chroma_key": args.chroma_key,
        "pet_notes": args.pet_notes,
        "style_preset": args.style_preset,
        "style_notes": args.style_notes,
        "style_contract": args.style_contract,
        "brand_name": "",
        "brand_brief": "",
        "brand_sources": [],
        "pet_safe_style": vendor.PET_SAFE_STYLE,
        "primary_generation_skill": "$imagegen",
    }
    request_path = run_dir / "pet_request.json"
    request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")

    vendor.write_text(prompt_dir / "base-pet.md", vendor.base_pet_prompt(args))
    for state, row, frames, purpose in vendor.ROWS:
        vendor.write_text(
            row_prompt_dir / f"{state}.md",
            vendor.row_prompt(args, state, row, frames, purpose),
        )
        vendor.write_text(
            row_retry_dir / f"{state}.md",
            vendor.retry_row_prompt(args, state, row, frames, purpose),
        )
    for state, row, directions, _purpose in vendor.LOOK_ROWS:
        vendor.write_text(row_prompt_dir / f"{state}.md", vendor.look_row_prompt(args, row, directions))
        vendor.write_text(
            row_retry_dir / f"{state}.md",
            vendor.retry_look_row_prompt(args, row, directions),
        )
    vendor.write_text(prompt_dir / "look-cardinals.md", vendor.look_cardinal_prompt(args))
    for label, expected_direction in vendor.LOOK_CARDINALS:
        vendor.write_text(
            repair_prompt_dir / f"{label}.md",
            vendor.look_cardinal_repair_prompt(args, label, expected_direction),
        )

    jobs_path = run_dir / "imagegen-jobs.json"
    jobs_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created_at": created_at,
                "run_dir": str(run_dir),
                "primary_generation_skill": "$imagegen",
                "jobs": vendor.make_jobs(run_dir, copied_refs),
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return PreparedRun(run_dir=run_dir, request=request_path, jobs=jobs_path)
