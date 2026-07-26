from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from omnipet.approvals import ACCEPTED_BASE_DECISION, validate_direction_checkpoint
from omnipet.diagnostics import SafeDiagnostic
from omnipet.generation import GroundingImage, ImageRequest
from omnipet.guides import (
    clear_generation_guides,
    load_generation_guides,
)
from omnipet.hatch.atlas import (
    AssembleExtendedAtlasConfig,
    ComposeAtlasConfig,
    assemble_extended_atlas,
    compose_atlas,
    compose_cardinal_anchor_strip,
)
from omnipet.hatch.directions import (
    ContinuityConfig,
    DirectionQaSheetConfig,
    make_direction_qa_sheet,
    measure_direction_continuity,
)
from omnipet.hatch.extract import (
    CardinalAnchorsConfig,
    ExtractStripFramesConfig,
    extract_cardinal_anchors,
    extract_strip_frames,
)
from omnipet.hatch.inspect import (
    ContactSheetConfig,
    InspectFramesConfig,
    PreviewConfig,
    inspect_frames,
    make_contact_sheet,
    render_animation_previews,
)
from omnipet.openai_images import OpenAIImageError, OpenAIImageGenerator
from omnipet.package import PackageError, build_package_evidence, import_package_verdict
from omnipet.project import PetProject, load_pet_project
from omnipet.run import EXPECTED_JOB_IDS, STANDARD_JOB_IDS, prepare_run
from omnipet.workflow import (
    WorkflowState,
    approve_workflow_stage,
    clear_blocked,
    mark_blocked,
    refresh_workflow,
)
from omnipet.workflow import _approve_workflow_stage_unlocked, _refresh_workflow_unlocked, _workflow_lock


GeneratorFactory = Callable[[PetProject], Any]
_CARDINALS = (("000", "up"), ("090", "right"), ("180", "down"), ("270", "left"))
_ROW9 = ("000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5")
_ROW10 = ("180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5")
_EXPECTED_PROMPTS = {
    "base": "prompts/base-pet.md",
    "look-cardinals": "prompts/look-cardinals.md",
    **{job_id: f"prompts/rows/{job_id}.md" for job_id in EXPECTED_JOB_IDS[1:10]},
    "look-row-9": "prompts/rows/look-row-9.md",
    "look-row-10": "prompts/rows/look-row-10.md",
}


class JobGenerationError(RuntimeError):
    def __init__(self, job_id: str, code: str, diagnostic: SafeDiagnostic):
        super().__init__("job generation failed")
        self.job_id = job_id
        self.code = code
        self.diagnostic = diagnostic


def init_pet_project(repo_root: Path, pet_id: str, *, standalone: Path | None = None) -> Path:
    root = _real_directory(repo_root)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", pet_id):
        raise ValueError("invalid pet id")
    destination = Path(standalone).absolute() if standalone is not None else root / "pets" / pet_id
    if destination.is_symlink():
        raise ValueError("pet destination is unsafe")
    if destination.exists():
        raise FileExistsError("pet destination already exists")
    parent = destination.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ValueError("pet destination parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.resolve() != parent.absolute():
        raise ValueError("pet destination parent is unsafe")
    try:
        with as_file(files("omnipet").joinpath("templates", "pet")) as template:
            shutil.copytree(template, destination, symlinks=False)
        manifest = destination / "pet.yaml"
        text = manifest.read_text(encoding="utf-8").replace("id: example-pet", f"id: {pet_id}")
        text = text.replace("display_name: Example Pet", f"display_name: {pet_id.replace('-', ' ').title()}")
        manifest.write_text(text, encoding="utf-8")
        load_pet_project(destination if standalone is not None else root, "." if standalone is not None else pet_id)
    except Exception:
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise
    return destination


def hatch_project(
    project: PetProject,
    *,
    generator_factory: GeneratorFactory = lambda project: OpenAIImageGenerator(
        model=project.image_generation_model, quality=project.image_generation_quality
    ),
) -> WorkflowState:
    with _hatch_lock(project):
        run_dir = prepare_run(project, project.repository_root).run_dir
        return _hatch_project_locked(project, run_dir, generator_factory)


def _hatch_project_locked(
    project: PetProject, run_dir: Path, generator_factory: GeneratorFactory
) -> WorkflowState:
    active_job: str | None = None
    try:
        workflow = refresh_workflow(run_dir)
        if workflow.state == "building_package":
            build_package_evidence(project)
            return refresh_workflow(run_dir)
        if workflow.state.startswith("awaiting_") or workflow.state in {"blocked", "complete"}:
            return workflow
        if workflow.state == "preparing":
            active_job = "base"
        elif workflow.state == "generating_standard_rows":
            active_job = _next_pending(run_dir, STANDARD_JOB_IDS)
        elif workflow.state == "generating_directions":
            active_job = _direction_action(run_dir)
        generator = generator_factory(project) if active_job is not None else None
        if workflow.state == "preparing":
            return _generate_base_candidate(project, run_dir, generator)
        if workflow.state == "generating_standard_rows":
            _generate_standard_rows(project, run_dir, generator)
            active_job = None
            _qa_standard(run_dir)
        elif workflow.state == "generating_directions":
            if active_job is not None:
                _generate_direction_action(project, run_dir, generator, active_job)
        return refresh_workflow(run_dir)
    except JobGenerationError as error:
        _fail_job(run_dir, error.job_id)
        return mark_blocked(
            run_dir, code=error.code, job=error.job_id, evidence=None,
            diagnostic=error.diagnostic,
        )
    except Exception as error:
        active_job = _running_job(run_dir) or active_job
        if active_job is not None:
            _fail_job(run_dir, active_job)
        code = "package-build-failed" if workflow.state == "building_package" else "generation-failed"
        diagnostic = (
            SafeDiagnostic(
                "deterministic-qa" if isinstance(error, PackageError) else "publication"
            )
            if workflow.state == "building_package"
            else _exception_diagnostic(
                error,
                "deterministic-qa"
                if workflow.state in {
                    "generating_standard_rows", "generating_directions"
                }
                else "local-validation",
            )
        )
        return mark_blocked(
            run_dir, code=code, job=active_job, evidence=None,
            diagnostic=diagnostic,
        )


def approve_project_stage(project: PetProject, stage: str, *, note: str | None = None) -> WorkflowState:
    run_dir = _run_dir(project)
    if stage == "base":
        with _hatch_lock(project), _workflow_lock(run_dir):
            return _approve_base_transaction(project, run_dir, note)
    with _hatch_lock(project):
        return approve_workflow_stage(run_dir, stage, note=note)


def _approve_base_transaction(project: PetProject, run_dir: Path, note: str | None) -> WorkflowState:
    paths = (
        project.root / "pet.yaml",
        project.root / "approved/canonical-base.png",
        run_dir / "decoded/base.png",
        run_dir / "references/canonical-base.png",
        run_dir / "imagegen-jobs.json",
        run_dir / "qa/base/review.json",
        run_dir / "qa/candidates/base.json",
        run_dir / "qa/approvals.json",
        run_dir / "workflow.json",
    )
    directories = {path.parent for path in paths}
    existing_directories = {path for path in directories if path.is_dir() and not path.is_symlink()}
    originals = {path: path.read_bytes() if path.is_file() and not path.is_symlink() else None for path in paths}
    try:
        if note is not None and (not isinstance(note, str) or not note.strip()):
            raise ValueError("approval note is invalid")
        if _refresh_workflow_unlocked(run_dir).state != "awaiting_base_approval":
            raise ValueError("workflow is not awaiting base approval")
        _validated_base_candidate(run_dir)
        _promote_base_candidate(project, run_dir)
        return _approve_workflow_stage_unlocked(run_dir, "base", note=note)
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _write_bytes_atomic(path, content)
        for directory in sorted(directories - existing_directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise


def qa_project_stage(
    project: PetProject,
    stage: str,
    *,
    verdict_file: Path | None = None,
) -> WorkflowState:
    run_dir = _run_dir(project)
    if stage == "base":
        _validated_base_candidate(run_dir)
    elif stage == "standard-rows":
        if verdict_file is None:
            _qa_standard(run_dir)
        else:
            _import_standard_verdict(run_dir, verdict_file)
    elif stage == "directions":
        if verdict_file is None:
            raise ValueError("direction semantic verdict is required")
        _import_direction_verdict(run_dir, verdict_file)
    elif stage == "package":
        if verdict_file is None:
            raise ValueError("package verdict is required")
        with _hatch_lock(project):
            import_package_verdict(project, verdict_file)
    else:
        raise ValueError("stage QA is not available")
    return refresh_workflow(run_dir)


def reset_failed_job(project: PetProject, job_id: str) -> WorkflowState:
    run_dir = _run_dir(project)
    with _hatch_lock(project):
        return _reset_failed_job_locked(run_dir, job_id)


def repair_project_job(project: PetProject, job_id: str, *, reason: str):
    from omnipet.repair import repair_completed_job

    return repair_completed_job(project, job_id, reason=reason)


def _reset_failed_job_locked(run_dir: Path, job_id: str) -> WorkflowState:
    state = refresh_workflow(run_dir)
    if state.blocked is None or state.blocked["job"] != job_id:
        raise ValueError("blocked job does not match reset")
    manifest = _read_json(run_dir / "imagegen-jobs.json")
    job = next((item for item in manifest["jobs"] if item.get("id") == job_id), None)
    if job is None or job.get("status") != "failed":
        raise ValueError("job is not failed")
    artifacts = _failed_job_artifacts(run_dir, job_id)
    archive_root = run_dir.parents[1] / "archives" / "failed-attempts"
    if archive_root.is_symlink() or (archive_root.exists() and not archive_root.is_dir()):
        raise ValueError("failed-attempt archive root is unsafe")
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = archive_root / f"{job_id}-{stamp}"
    staging = Path(tempfile.mkdtemp(prefix=f".{job_id}-", dir=archive_root))
    original_manifest = (run_dir / "imagegen-jobs.json").read_bytes()
    workflow_path = run_dir / "workflow.json"
    original_workflow = workflow_path.read_bytes()
    moved: list[tuple[Path, Path]] = []
    try:
        for source in artifacts:
            relative = source.relative_to(run_dir)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append((source, destination))
        job["status"] = "pending"
        for key in ("source_path", "completed_at", "derived_from", "mirror_decision"):
            job.pop(key, None)
        metadata = job.get("metadata")
        if isinstance(metadata, dict):
            for key in ("attempts", "started_at"):
                metadata.pop(key, None)
            if not metadata:
                job.pop("metadata", None)
        _write_json(run_dir / "imagegen-jobs.json", manifest)
        result = clear_blocked(run_dir)
        os.replace(staging, archive)
        return result
    except Exception:
        _write_bytes_atomic(run_dir / "imagegen-jobs.json", original_manifest)
        _write_bytes_atomic(workflow_path, original_workflow)
        for source, archived in reversed(moved):
            if archived.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archived, source)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _failed_job_artifacts(run_dir: Path, job_id: str) -> tuple[Path, ...]:
    candidates = [
        run_dir / "generated-sources" / f"{job_id}.png",
        run_dir / "decoded" / f"{job_id}.png",
        run_dir / "qa" / "rows" / job_id,
        run_dir / "qa" / "visual-jobs" / f"{job_id}.result.json",
        run_dir / "previews" / f"{job_id}.gif",
    ]
    if job_id == "base":
        candidates.extend((run_dir / "qa/candidates/base.json", run_dir / "qa/base"))
    elif job_id == "look-cardinals":
        candidates.extend((run_dir / "qa/directions/cardinals", run_dir / "decoded/look-anchors"))
    elif job_id in {"look-row-9", "look-row-10"}:
        candidates.extend(sorted((run_dir / "qa/directions").glob(f"{job_id}*")))
        if job_id == "look-row-10":
            candidates.extend((
                run_dir / "qa/directions/continuity.json",
                run_dir / "qa/directions/contact-sheet.png",
                run_dir / "final/spritesheet-extended.png",
            ))
    result = []
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(run_dir.resolve()):
            raise ValueError("failed job artifact is unsafe")
        if path.is_dir() and any(child.is_symlink() for child in path.rglob("*")):
            raise ValueError("failed job artifact contains a symlink")
        if not path.is_file() and not path.is_dir():
            raise ValueError("failed job artifact is invalid")
        result.append(path)
    return tuple(result)


def clear_project_block(project: PetProject) -> WorkflowState:
    run_dir = _run_dir(project)
    with _hatch_lock(project):
        return _clear_project_block_locked(run_dir)


def _clear_project_block_locked(run_dir: Path) -> WorkflowState:
    state = refresh_workflow(run_dir)
    if state.blocked is None or state.blocked["job"] is not None:
        raise ValueError("only an aggregate block can be cleared")
    return clear_blocked(run_dir)


def project_status(project: PetProject) -> dict[str, Any]:
    run_dir = project.repository_root / ".omnipet" / "runs" / project.pet_id
    state = refresh_workflow(run_dir) if run_dir.is_dir() else WorkflowState("preparing")
    phase = _direction_phase(run_dir) if state.state in {
        "generating_directions", "awaiting_directions_approval"
    } else None
    selector = "." if project.root == project.repository_root else project.pet_id
    standard_complete = _jobs_complete(run_dir, STANDARD_JOB_IDS)
    package_generated = (run_dir / "qa/package-generated/blind-sheet.png").is_file()
    actions = {
        "preparing": f"omnipet hatch {selector}",
        "awaiting_base_approval": f"omnipet approve {selector} --stage base",
        "generating_standard_rows": (
            f"omnipet qa {selector} --stage standard-rows --verdict-file standard-verdict.json"
            if standard_complete
            else f"omnipet hatch {selector}"
        ),
        "awaiting_standard_rows_approval": f"omnipet approve {selector} --stage standard-rows",
        "generating_directions": (
            f"omnipet qa {selector} --stage directions --verdict-file direction-verdict.json"
            if phase and phase.endswith("awaiting_review")
            else f"omnipet hatch {selector}"
        ),
        "awaiting_directions_approval": f"omnipet approve {selector} --stage directions",
        "building_package": (
            f"omnipet qa {selector} --stage package --verdict-file package-verdict.json"
            if package_generated
            else f"omnipet hatch {selector}"
        ),
        "awaiting_package_approval": (
            f"omnipet package {selector}"
            if _package_is_approved(run_dir)
            else f"omnipet approve {selector} --stage package"
        ),
        "complete": "none",
        "blocked": (
            f"omnipet hatch {selector} --reset-failed {state.blocked['job']}"
            if state.blocked and state.blocked["job"] is not None
            else f"omnipet hatch {selector} --clear-block"
        ),
    }
    next_action = actions[state.state]
    if len(next_action) > 256:
        next_action = "none"
    return {
        "ok": True,
        "pet_id": project.pet_id,
        "run_dir": str(run_dir),
        "workflow_state": state.state,
        "direction_phase": phase,
        "next_action": next_action,
        "blocked": state.blocked,
    }


def _jobs_complete(run_dir: Path, job_ids: tuple[str, ...]) -> bool:
    if not run_dir.is_dir():
        return False
    try:
        jobs = _read_json(run_dir / "imagegen-jobs.json")["jobs"]
        statuses = {job["id"]: job["status"] for job in jobs}
        return all(statuses.get(job_id) == "complete" for job_id in job_ids)
    except (OSError, KeyError, TypeError, ValueError):
        return False


def _package_is_approved(run_dir: Path) -> bool:
    if not run_dir.is_dir():
        return False
    from omnipet.approvals import load_approvals
    try:
        return any(record.stage == "package" for record in load_approvals(run_dir))
    except Exception:
        return False


def _generate_base_candidate(project: PetProject, run_dir: Path, generator: Any) -> WorkflowState:
    candidate_path = run_dir / "qa/candidates/base.json"
    if candidate_path.exists():
        _validated_base_candidate(run_dir)
        return refresh_workflow(run_dir)
    source = _generate_source(project, run_dir, generator, "base")
    _validate_png(source)
    _write_json(candidate_path, {
        "schema_version": 1,
        "job_id": "base",
        "source_path": "generated-sources/base.png",
        "sha256": _sha256(source),
        "canvas": {"aspect_ratio": "1:1", "image_size": "1K"},
    })
    _set_job_status(run_dir, "base", "pending")
    clear_generation_guides(run_dir, "base")
    return refresh_workflow(run_dir)


def _validated_base_candidate(run_dir: Path) -> tuple[dict[str, Any], Path]:
    candidate = _read_json(run_dir / "qa/candidates/base.json")
    if set(candidate) != {"schema_version", "job_id", "source_path", "sha256", "canvas"} or (
        candidate.get("schema_version") != 1
        or candidate.get("job_id") != "base"
        or candidate.get("source_path") != "generated-sources/base.png"
        or candidate.get("canvas") != {"aspect_ratio": "1:1", "image_size": "1K"}
    ):
        raise ValueError("base candidate is invalid")
    source = _safe_run_file(run_dir, candidate["source_path"], expected="generated-sources/base.png")
    _validate_png(source)
    if candidate.get("sha256") != _sha256(source):
        raise ValueError("base candidate changed")
    return candidate, source


def _promote_base_candidate(project: PetProject, run_dir: Path) -> None:
    candidate, source = _validated_base_candidate(run_dir)
    for destination in (
        run_dir / "decoded/base.png",
        run_dir / "references/canonical-base.png",
        project.root / "approved/canonical-base.png",
    ):
        _copy_atomic(source, destination)
    manifest = project.root / "pet.yaml"
    text = manifest.read_text(encoding="utf-8")
    if "\napproved:\n" not in text:
        _write_text_atomic(manifest, text.rstrip() + "\napproved:\n  canonical_base: approved/canonical-base.png\n")
    completed = datetime.now(timezone.utc).isoformat()
    _set_job_status(run_dir, "base", "running")
    _complete_job(run_dir, "base", source, completed)
    _write_json(run_dir / "qa/base/review.json", {
        "adoption_decision": ACCEPTED_BASE_DECISION,
        "canvas": candidate["canvas"],
        "completed_at": completed,
        "job_id": "base",
        "ok": True,
        "sha256": candidate["sha256"],
    })


def _generate_standard_rows(project: PetProject, run_dir: Path, generator: Any) -> None:
    for job_id in STANDARD_JOB_IDS:
        if _job(run_dir, job_id)["status"] == "complete":
            continue
        try:
            source = _generate_source(project, run_dir, generator, job_id)
            _stage_generated_row(run_dir, job_id, source)
        except JobGenerationError:
            raise
        except Exception as error:
            raise JobGenerationError(
                job_id, "generation-failed",
                _exception_diagnostic(error, "deterministic-qa"),
            ) from None


def _generate_direction_action(project: PetProject, run_dir: Path, generator: Any, job_id: str) -> None:
    try:
        source = _generate_source(project, run_dir, generator, job_id)
        decoded = run_dir / "decoded" / f"{job_id}.png"
        _copy_atomic(source, decoded)
        if job_id == "look-cardinals":
            _qa_cardinals(run_dir)
        elif job_id == "look-row-9":
            _qa_row9(run_dir)
        else:
            _qa_row10(run_dir)
    except JobGenerationError:
        (run_dir / "decoded" / f"{job_id}.png").unlink(missing_ok=True)
        raise
    except Exception as error:
        (run_dir / "decoded" / f"{job_id}.png").unlink(missing_ok=True)
        raise JobGenerationError(
            job_id, "generation-failed",
            _exception_diagnostic(error, "deterministic-qa"),
        ) from None
    _complete_job(run_dir, job_id, source, datetime.now(timezone.utc).isoformat())


def _generate_source(project: PetProject, run_dir: Path, generator: Any, job_id: str) -> Path:
    job = _job(run_dir, job_id)
    _validate_manifest_inputs(run_dir, job_id, job)
    expected_prompt = _EXPECTED_PROMPTS[job_id]
    prompt_path = _safe_run_file(run_dir, job.get("prompt_file"), expected=expected_prompt)
    metadata = job.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("prompt_sha256") != _sha256(prompt_path)
    ):
        raise ValueError("job prompt changed")
    if job.get("output_path") != f"decoded/{job_id}.png":
        raise ValueError("job output path is invalid")
    prompt = prompt_path.read_text(encoding="utf-8")
    destination = run_dir / "generated-sources" / f"{job_id}.png"
    guide_records = load_generation_guides(run_dir, job_id)
    grounding = _grounding(run_dir, job_id, guide_records)
    canvas = job["canvas"]
    _begin_attempt(run_dir, job_id, guide_records)
    request = ImageRequest(
        prompt=prompt,
        destination=destination,
        run_root=run_dir,
        grounding_images=grounding,
        aspect_ratio=canvas["aspect_ratio"],
        image_size=canvas["image_size"],
        task=job_id,
    )
    try:
        generated = generator.edit(request) if grounding else generator.generate(request)
    except Exception as error:
        raise JobGenerationError(
            job_id, "generation-failed",
            _exception_diagnostic(error, "provider-request"),
        ) from None
    try:
        source = Path(generated.path)
        if source != destination or source.is_symlink():
            raise ValueError("generated source path is invalid")
        _validate_png(source)
    except Exception:
        raise JobGenerationError(
            job_id, "generation-failed", SafeDiagnostic("deterministic-qa")
        ) from None
    return source


def _exception_diagnostic(error: Exception, fallback: str) -> SafeDiagnostic:
    if isinstance(error, OpenAIImageError):
        return error.diagnostic
    return SafeDiagnostic(fallback)


def _validate_manifest_inputs(run_dir: Path, job_id: str, job: dict[str, Any]) -> None:
    values = job.get("input_images")
    if not isinstance(values, list):
        raise ValueError("job inputs are invalid")
    for item in values:
        if not isinstance(item, dict) or set(item) != {"path", "role"} or not isinstance(item["role"], str) or not item["role"].strip():
            raise ValueError("job input record is invalid")
        path = item.get("path")
        if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError("job input path is invalid")
        if path.startswith("references/reference-"):
            _safe_run_file(run_dir, path)
        elif path.startswith("references/layout-guides/"):
            expected = f"references/layout-guides/{job_id}.png"
            _safe_run_file(run_dir, path, expected=expected)
        elif path == "references/canonical-base.png":
            if job_id == "base":
                raise ValueError("base input contract is invalid")
        elif path in {"qa/standard/contact-sheet.png", "decoded/look-cardinals-approved.png"}:
            if job_id not in {"look-cardinals", "look-row-9", "look-row-10"}:
                raise ValueError("direction input contract is invalid")
        elif path == "decoded/look-row-9.png":
            if job_id != "look-row-10":
                raise ValueError("direction continuity input is invalid")
        elif path == "decoded/running-right.png":
            if job_id != "running-left":
                raise ValueError("running input contract is invalid")
        else:
            raise ValueError("unexpected job input path")
    paths = {item["path"] for item in values}
    metadata = _read_json(run_dir / "omnipet-run.json")
    expected_references = {item["run_path"] for item in metadata["references"]}
    if not expected_references.issubset(paths):
        raise ValueError("prepared reference input is missing")
    if job_id != "base":
        required = {
            f"references/layout-guides/{job_id}.png",
            "references/canonical-base.png",
        }
        if job_id in {"look-cardinals", "look-row-9", "look-row-10"}:
            required.add("qa/standard/contact-sheet.png")
        if job_id in {"look-row-9", "look-row-10"}:
            required.add("decoded/look-cardinals-approved.png")
        if job_id == "look-row-10":
            required.add("decoded/look-row-9.png")
        if job_id == "running-left":
            required.add("decoded/running-right.png")
        if not required.issubset(paths):
            raise ValueError("required job input is missing")


def _grounding(
    run_dir: Path,
    job_id: str,
    guide_records: tuple[dict[str, str], ...] | None = None,
) -> tuple[GroundingImage, ...]:
    paths: list[tuple[Path, str]] = []
    metadata = _read_json(run_dir / "omnipet-run.json")
    references = []
    for index, item in enumerate(metadata["references"], 1):
        expected = f"references/reference-{index:02d}{Path(item['run_path']).suffix.lower()}"
        path = _safe_run_file(run_dir, item["run_path"], expected=expected)
        if item.get("sha256") != _sha256(path):
            raise ValueError("prepared reference changed")
        references.append((path, item["role"]))
    if job_id == "base":
        paths.extend(references)
    else:
        paths.extend(references)
        paths.append((_safe_run_file(run_dir, "references/canonical-base.png"), "canonical identity"))
        guide = run_dir / "references/layout-guides" / f"{job_id}.png"
        if guide.is_file() and not guide.is_symlink():
            paths.append((guide, "layout only"))
        if job_id in {"look-cardinals", "look-row-9", "look-row-10"}:
            paths.append((_safe_run_file(run_dir, "qa/standard/contact-sheet.png"), "approved standard contact sheet"))
        if job_id in {"look-row-9", "look-row-10"}:
            paths.append((_safe_run_file(run_dir, "decoded/look-cardinals-approved.png"), "approved cardinal anchors"))
        if job_id == "look-row-10":
            paths.append((_safe_run_file(run_dir, "decoded/look-row-9.png"), "completed first direction row"))
    records = (
        load_generation_guides(run_dir, job_id)
        if guide_records is None
        else guide_records
    )
    snapshots = [_snapshot(path, role) for path, role in paths]
    for record in records:
        snapshot = _snapshot(
            _safe_run_file(run_dir, record["path"]),
            _provider_guide_role(record),
        )
        if snapshot.content_sha256 != record["sha256"]:
            raise ValueError("registered guide changed")
        snapshots.append(snapshot)
    return tuple(snapshots)


def _provider_guide_role(record: dict[str, str]) -> str:
    authoritative = "true" if record["authority"] == "identity" else "false"
    return (
        f"authority={record['authority']}; "
        f"identity_authoritative={authoritative}; "
        f"role={json.dumps(record['role'])}"
    )


def _snapshot(path: Path, role: str) -> GroundingImage:
    data = path.read_bytes()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[path.suffix.lower()]
    return GroundingImage(path, role, data, mime, hashlib.sha256(data).hexdigest())


def _stage_generated_row(run_dir: Path, job_id: str, source: Path) -> None:
    decoded = run_dir / "decoded" / f"{job_id}.png"
    _copy_atomic(source, decoded)
    try:
        if job_id in STANDARD_JOB_IDS:
            _qa_row(run_dir, job_id)
    except Exception:
        decoded.unlink(missing_ok=True)
        raise
    _complete_job(run_dir, job_id, source, datetime.now(timezone.utc).isoformat())


def _qa_row(run_dir: Path, job_id: str) -> None:
    root = run_dir / "qa/rows" / job_id / "frames"
    extract_strip_frames(ExtractStripFramesConfig(run_dir / "decoded", root, states=(job_id,), method="auto"))
    result = inspect_frames(InspectFramesConfig(
        root,
        run_dir / "qa/rows" / job_id / "deterministic.json",
        states=(job_id,),
        require_components=True,
    ))
    if not result.ok:
        raise ValueError("row QA failed")


def _qa_standard(run_dir: Path) -> None:
    if any(_job(run_dir, job_id)["status"] != "complete" for job_id in STANDARD_JOB_IDS):
        raise ValueError("standard rows are incomplete")
    frames = run_dir / "frames"
    extract_strip_frames(ExtractStripFramesConfig(run_dir / "decoded", frames, states=STANDARD_JOB_IDS, method="auto"))
    result = inspect_frames(InspectFramesConfig(
        frames,
        run_dir / "qa/standard/review.json",
        states=STANDARD_JOB_IDS,
        require_components=True,
    ))
    if not result.ok:
        raise ValueError("standard QA failed")
    atlas = compose_atlas(ComposeAtlasConfig(
        run_dir / "final/spritesheet.png",
        frames_root=frames,
        webp_output=run_dir / "final/spritesheet.webp",
    ))
    previews = render_animation_previews(PreviewConfig(frames, run_dir / "previews"))
    make_contact_sheet(ContactSheetConfig(atlas.webp_output, run_dir / "qa/standard/contact-sheet.png"))
    if len(previews.previews) != len(STANDARD_JOB_IDS) or any(path.stat().st_size == 0 for path in previews.previews):
        raise ValueError("standard previews are incomplete")
    _write_json(run_dir / "qa/standard/previews.json", {
        "ok": True,
        "previews": [str(path.relative_to(run_dir)) for path in previews.previews],
    })


def _qa_cardinals(run_dir: Path) -> None:
    chroma = _read_json(run_dir / "pet_request.json")["chroma_key"]["hex"]
    anchors = extract_cardinal_anchors(CardinalAnchorsConfig(
        run_dir / "decoded/look-cardinals.png",
        run_dir / "decoded/look-anchors",
        run_dir / "qa/directions/cardinals/deterministic.json",
        chroma,
    ))
    if not anchors.ok:
        raise ValueError("cardinal extraction failed")
    compose_cardinal_anchor_strip(anchors_dir=anchors.anchors[0].parent, output=run_dir / "qa/directions/cardinals/sheet.png")


def _qa_row9(run_dir: Path) -> None:
    chroma = _read_json(run_dir / "pet_request.json")["chroma_key"]["hex"]
    result = assemble_extended_atlas(AssembleExtendedAtlasConfig(
        base_atlas=run_dir / "final/spritesheet.webp",
        look_row_9=run_dir / "decoded/look-row-9.png",
        neutral_cell=run_dir / "frames/idle/00.png",
        registered_row_output=run_dir / "qa/directions/look-row-9-registered.png",
        registration_manifest_output=run_dir / "qa/directions/look-row-9-registration.json",
        chroma_key=chroma,
    ))
    atlas = assemble_extended_atlas(AssembleExtendedAtlasConfig(
        base_atlas=run_dir / "final/spritesheet.webp",
        output=run_dir / "qa/directions/look-row-9-preview-atlas.png",
        registered_row_9=result.output,
        row_9_registration=result.manifest,
        look_row_10=run_dir / "decoded/look-row-9.png",
        neutral_cell=run_dir / "frames/idle/00.png",
        chroma_key=chroma,
    ))
    continuity = measure_direction_continuity(ContinuityConfig(
        atlas.output,
        run_dir / "qa/directions/look-row-9-continuity.json",
    ))
    if continuity.report.get("ok") is not True:
        raise ValueError("row 9 continuity failed")
    make_direction_qa_sheet(DirectionQaSheetConfig(
        atlas.output,
        run_dir / "qa/directions/look-row-9-contact-sheet.png",
    ))


def _qa_row10(run_dir: Path) -> None:
    chroma = _read_json(run_dir / "pet_request.json")["chroma_key"]["hex"]
    extended = assemble_extended_atlas(AssembleExtendedAtlasConfig(
        base_atlas=run_dir / "final/spritesheet.webp",
        registered_row_9=run_dir / "qa/directions/look-row-9-registered.png",
        row_9_registration=run_dir / "qa/directions/look-row-9-registration.json",
        look_row_10=run_dir / "decoded/look-row-10.png",
        neutral_cell=run_dir / "frames/idle/00.png",
        output=run_dir / "final/spritesheet-extended.png",
        chroma_key=chroma,
    ))
    row9_registration = _read_json(run_dir / "qa/directions/look-row-9-registration.json")
    _write_json(run_dir / "qa/directions/look-row-10-registration.json", {
        "ok": True,
        "scale": row9_registration["scale"],
        "source_sha256": _sha256(run_dir / "decoded/look-row-10.png"),
        "registered_row_9_sha256": _sha256(run_dir / "qa/directions/look-row-9-registered.png"),
        "atlas_sha256": _sha256(extended.output),
    })
    continuity = measure_direction_continuity(ContinuityConfig(
        extended.output,
        run_dir / "qa/directions/continuity.json",
    ))
    if continuity.report.get("ok") is not True:
        raise ValueError("direction continuity failed")
    make_direction_qa_sheet(DirectionQaSheetConfig(extended.output, run_dir / "qa/directions/contact-sheet.png"))


def _import_standard_verdict(run_dir: Path, verdict_file: Path) -> None:
    verdict = _external_json(verdict_file)
    rows = verdict.get("rows") if isinstance(verdict, dict) else None
    if set(verdict) != {"schema_version", "stage", "rows"} or verdict.get("schema_version") != 1 or verdict.get("stage") != "standard-rows" or not isinstance(rows, list):
        raise ValueError("standard verdict schema is invalid")
    if [row.get("id") for row in rows if isinstance(row, dict)] != list(STANDARD_JOB_IDS):
        raise ValueError("standard verdict rows are invalid")
    for row in rows:
        if set(row) != {"id", "verdict", "note", "evidence"} or row["verdict"] != "pass" or not isinstance(row["note"], str) or not row["note"].strip():
            raise ValueError("standard row verdict is not accepted")
        evidence = _verify_evidence(run_dir, row["evidence"], (
            f"generated-sources/{row['id']}.png",
            f"decoded/{row['id']}.png",
            f"qa/rows/{row['id']}/deterministic.json",
            f"previews/{row['id']}.gif",
        ))
        _write_json(run_dir / "qa/rows" / row["id"] / "review.json", {"ok": True, **row, "evidence": evidence})


def _import_direction_verdict(run_dir: Path, verdict_file: Path) -> None:
    verdict = _external_json(verdict_file)
    phase = verdict.get("phase") if isinstance(verdict, dict) else None
    expected_phase = {
        "cardinals_awaiting_review": "cardinals",
        "row9_awaiting_review": "row9",
        "row10_awaiting_review": "row10",
    }.get(_direction_phase(run_dir))
    if set(verdict) != {"schema_version", "stage", "phase", "directions", "evidence"} or verdict.get("schema_version") != 1 or verdict.get("stage") != "directions" or phase != expected_phase:
        raise ValueError("direction verdict schema is invalid")
    directions = verdict.get("directions")
    expected = _CARDINALS if phase == "cardinals" else tuple((item, None) for item in (_ROW9 if phase == "row9" else _ROW10))
    if not isinstance(directions, list) or len(directions) != len(expected):
        raise ValueError("direction verdict count is invalid")
    normalized = []
    for item, (label, semantic) in zip(directions, expected, strict=True):
        keys = {"direction", "verdict", "note"} | ({"expected"} if semantic else set())
        if not isinstance(item, dict) or set(item) != keys or item.get("direction") != label or item.get("verdict") != "pass" or not isinstance(item.get("note"), str) or not item["note"].strip() or (semantic and item.get("expected") != semantic):
            raise ValueError("direction semantic verdict is not accepted")
        normalized.append(item)
    if phase == "cardinals":
        evidence = _verify_evidence(run_dir, verdict["evidence"], (
            "generated-sources/look-cardinals.png",
            "decoded/look-cardinals.png",
            "qa/directions/cardinals/deterministic.json",
            "qa/directions/cardinals/sheet.png",
            "decoded/look-cardinals-approved.png",
        ), substitutions={"decoded/look-cardinals-approved.png": "qa/directions/cardinals/sheet.png"})
        _copy_atomic(run_dir / "qa/directions/cardinals/sheet.png", run_dir / "decoded/look-cardinals-approved.png")
        _write_json(run_dir / "qa/directions/cardinals/review.json", {
            "ok": True, "directions": normalized, "evidence": evidence,
        })
    else:
        evidence_paths = (
            (
                "generated-sources/look-row-9.png",
                "decoded/look-row-9.png",
                "qa/directions/look-row-9-registered.png",
                "qa/directions/look-row-9-registration.json",
                "qa/directions/look-row-9-continuity.json",
                "qa/directions/look-row-9-contact-sheet.png",
            ) if phase == "row9" else (
                "generated-sources/look-row-10.png",
                "decoded/look-row-10.png",
                "qa/directions/look-row-10-registration.json",
                "qa/directions/continuity.json",
                "qa/directions/contact-sheet.png",
            )
        )
        evidence = _verify_evidence(run_dir, verdict["evidence"], evidence_paths)
        path = run_dir / "qa/directions/direction-semantics.json"
        existing = _read_json(path) if path.is_file() else {"schema_version": 1, "reviews": {}}
        existing["reviews"][phase] = {"directions": normalized, "evidence": evidence}
        existing["ok"] = set(existing["reviews"]) == {"row9", "row10"}
        _write_json(path, existing)


def _direction_phase(run_dir: Path) -> str:
    if not run_dir.is_dir():
        return "cardinals_pending"
    statuses = {job_id: _job(run_dir, job_id)["status"] for job_id in EXPECTED_JOB_IDS[10:]}
    if statuses["look-cardinals"] != "complete":
        return "cardinals_pending"
    if not (run_dir / "qa/directions/cardinals/review.json").is_file():
        return "cardinals_awaiting_review"
    validate_direction_checkpoint(run_dir, "cardinals")
    if statuses["look-row-9"] != "complete":
        return "row9_pending"
    semantics = run_dir / "qa/directions/direction-semantics.json"
    reviews = _read_json(semantics).get("reviews", {}) if semantics.is_file() else {}
    if "row9" not in reviews:
        return "row9_awaiting_review"
    validate_direction_checkpoint(run_dir, "row9")
    if statuses["look-row-10"] != "complete":
        return "row10_pending"
    if "row10" not in reviews:
        return "row10_awaiting_review"
    return "directions_awaiting_stage_approval"


def _direction_action(run_dir: Path) -> str | None:
    return {
        "cardinals_pending": "look-cardinals",
        "row9_pending": "look-row-9",
        "row10_pending": "look-row-10",
    }.get(_direction_phase(run_dir))


def _next_pending(run_dir: Path, job_ids: tuple[str, ...]) -> str | None:
    return next((job_id for job_id in job_ids if _job(run_dir, job_id)["status"] != "complete"), None)


def _begin_attempt(
    run_dir: Path,
    job_id: str,
    guide_records: tuple[dict[str, str], ...] = (),
) -> None:
    manifest = _read_json(run_dir / "imagegen-jobs.json")
    job = next(item for item in manifest["jobs"] if item["id"] == job_id)
    if job["status"] != "pending":
        raise ValueError("job is not pending")
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    metadata["attempts"] = int(metadata.get("attempts", 0)) + 1
    metadata["started_at"] = datetime.now(timezone.utc).isoformat()
    metadata["generation_guides"] = [dict(record) for record in guide_records]
    job.update({"status": "running", "metadata": metadata})
    _write_json(run_dir / "imagegen-jobs.json", manifest)


def _set_job_status(run_dir: Path, job_id: str, status: str) -> None:
    manifest = _read_json(run_dir / "imagegen-jobs.json")
    next(item for item in manifest["jobs"] if item["id"] == job_id)["status"] = status
    _write_json(run_dir / "imagegen-jobs.json", manifest)


def _fail_job(run_dir: Path, job_id: str) -> None:
    _set_job_status(run_dir, job_id, "failed")
    clear_generation_guides(run_dir, job_id)


def _complete_job(run_dir: Path, job_id: str, source: Path, completed: str) -> None:
    manifest = _read_json(run_dir / "imagegen-jobs.json")
    job = next(item for item in manifest["jobs"] if item["id"] == job_id)
    if job["status"] != "running":
        raise ValueError("job is not running")
    job.update({"status": "complete", "source_path": str(source), "completed_at": completed})
    _write_json(run_dir / "imagegen-jobs.json", manifest)
    clear_generation_guides(run_dir, job_id)


def _running_job(run_dir: Path) -> str | None:
    return next((job_id for job_id in EXPECTED_JOB_IDS if _job(run_dir, job_id)["status"] == "running"), None)


def _job(run_dir: Path, job_id: str) -> dict[str, Any]:
    return next(item for item in _read_json(run_dir / "imagegen-jobs.json")["jobs"] if item["id"] == job_id)


def _safe_run_file(run_dir: Path, value: Any, *, expected: str | None = None) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts or (expected is not None and value != expected):
        raise ValueError("run input path is invalid")
    path = run_dir / value
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(run_dir):
        raise ValueError("run input is unsafe")
    return path.resolve()


def _external_json(path: Path) -> dict[str, Any]:
    value = Path(path).absolute()
    if value.is_symlink() or not value.is_file():
        raise ValueError("verdict file is unsafe")
    payload = json.loads(value.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("verdict must be an object")
    return payload


def _verify_evidence(
    run_dir: Path,
    evidence: Any,
    expected_paths: tuple[str, ...],
    *,
    substitutions: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    if not isinstance(evidence, list) or len(evidence) != len(expected_paths):
        raise ValueError("verdict evidence is invalid")
    normalized = []
    substitutions = substitutions or {}
    for item, relative in zip(evidence, expected_paths, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or item.get("path") != relative
            or not isinstance(item.get("sha256"), str)
        ):
            raise ValueError("verdict evidence is invalid")
        actual = _safe_run_file(run_dir, substitutions.get(relative, relative))
        if item["sha256"] != _sha256(actual):
            raise ValueError("verdict evidence changed")
        normalized.append({"path": relative, "sha256": item["sha256"]})
    return normalized


def _run_dir(project: PetProject) -> Path:
    path = project.repository_root / ".omnipet/runs" / project.pet_id
    if path.is_symlink() or not path.is_dir():
        raise ValueError("run is missing")
    return path


def _real_directory(path: Path) -> Path:
    value = Path(path).absolute()
    if value.is_symlink() or not value.is_dir() or value.resolve() != value:
        raise ValueError("repository root is unsafe")
    return value


def _validate_png(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("image is unsafe")
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError("image is not PNG")
            image.verify()
    except (OSError, UnidentifiedImageError):
        raise ValueError("image is invalid") from None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}-", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("JSON artifact is unsafe")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text_atomic(path: Path, text: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _hatch_lock(project: PetProject):
    root = project.repository_root / ".omnipet"
    locks = root / "locks"
    for directory in (root, locks):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("hatch lock directory is unsafe")
        directory.mkdir(exist_ok=True)
        if directory.resolve() != directory.absolute():
            raise ValueError("hatch lock directory is unsafe")
    path = locks / f"{project.pet_id}.hatch.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("hatch lock path is unsafe")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
