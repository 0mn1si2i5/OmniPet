from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

from omnipet.canvas import canvas_for_job, validate_job_canvas
from omnipet.hatch import HatchExecutionError
from omnipet.hatch.prepare import PrepareRunInputs, prepare_run as prepare_hatch_run
from omnipet.project import PetProject
from omnipet.security import contains_credential_like_text, is_credential_like_key
from omnipet._vendor.hatch.scripts.prepare_pet_run import (
    look_row_axis_contract,
    look_row_boundary_contract,
    look_row_layout_contract,
    look_row_pre_return_check,
    look_row_screen_coordinate_contract,
)


EXPECTED_JOB_IDS = (
    "base",
    "idle",
    "running-right",
    "running-left",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
    "look-cardinals",
    "look-row-9",
    "look-row-10",
)

STANDARD_JOB_IDS = EXPECTED_JOB_IDS[1:10]
EXPECTED_DEPENDENCIES = {
    "base": (),
    "idle": ("base",),
    "running-right": ("base",),
    "running-left": ("base", "running-right"),
    "waving": ("base",),
    "jumping": ("base",),
    "failed": ("base",),
    "waiting": ("base",),
    "running": ("base",),
    "review": ("base",),
    "look-cardinals": STANDARD_JOB_IDS,
    "look-row-9": ("look-cardinals",),
    "look-row-10": ("look-cardinals", "look-row-9"),
}

_MANIFEST_KEYS = {"schema_version", "created_at", "run_dir", "primary_generation_skill", "jobs"}
_JOB_KEYS = {
    "id", "kind", "status", "prompt_file", "retry_prompt_file", "repair_prompt_files",
    "repair_retry_prompt_files",
    "input_images", "output_path", "extracted_output_paths", "approved_strip_path",
    "depends_on", "generation_skill", "requires_grounded_generation",
    "allow_prompt_only_generation", "identity_reference_paths", "parallelizable_after",
    "derivation_policy", "mirror_policy", "look_mechanics_file", "directions",
    "packaging_eligible", "coherent_synthesis_required", "individual_cell_packaging_allowed",
    "source_path", "completed_at", "derived_from", "mirror_decision", "metadata",
    "repair_source_paths", "canvas", "adoption_decision",
}
class RunPreparationError(RuntimeError):
    """Raised when run preparation cannot produce validated resumable state."""


@dataclass(frozen=True)
class RunStage:
    name: str
    status: str


@dataclass(frozen=True)
class RunState:
    pet_id: str
    run_dir: Path
    job_ids: tuple[str, ...]
    counts: Mapping[str, int]
    stages: tuple[RunStage, ...]


def prepare_run(
    project: PetProject,
    repo_root: Path,
) -> RunState | Any:
    repo_root, runs_root, run_dir = _run_paths(repo_root, project.pet_id)
    if run_dir.exists() and any(run_dir.iterdir()):
        try:
            workflow = _read_json_document(run_dir / "workflow.json")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            workflow = None
        if isinstance(workflow, dict) and workflow.get("schema_version") == 2:
            from omnipet.workflow import load_workflow_v2

            return load_workflow_v2(run_dir)
        state = load_run_state(repo_root, project.pet_id, display_name=project.display_name)
        _validate_references(project, run_dir)
        _validate_canonical_base(project, run_dir)
        return state

    _create_runtime_parents(repo_root, runs_root)
    if run_dir.exists():
        raise RunPreparationError("empty run destination already exists")
    if project.agent_workflow_version == 2:
        from omnipet.release import initialize_design_run

        return initialize_design_run(run_dir, project.pet_id, project.references)
    run_dir.mkdir()
    try:
        prepare_hatch_run(
            PrepareRunInputs(
                pet_id=project.pet_id,
                display_name=project.display_name,
                description=project.description,
                style_preset=project.style_preset,
                style_notes=project.style_notes,
                output_dir=run_dir,
                references=tuple(project.reference_paths),
            )
        )
        _upgrade_manifest_canvas(run_dir / "imagegen-jobs.json")
        _load_state_at(run_dir, project.pet_id, project.display_name)
        reference_metadata = _reference_metadata(project, run_dir)
        _atomic_write_json(
            run_dir / "omnipet-run.json",
            {
                "schema_version": 1,
                "pet_id": project.pet_id,
                "references": reference_metadata,
            },
        )
        if project.canonical_base_path is not None:
            destination = run_dir / "references" / "canonical-base.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_validated_copy(project.canonical_base_path, destination)
        return load_run_state(repo_root, project.pet_id, display_name=project.display_name)
    except RunPreparationError:
        _remove_failed_run(runs_root, run_dir)
        raise
    except (HatchExecutionError, OSError, ValueError):
        _remove_failed_run(runs_root, run_dir)
        raise RunPreparationError("built-in run preparation failed") from None


def load_run_state(repo_root: Path, pet_id: str, *, display_name: str | None = None) -> RunState:
    _repo_root, _runs_root, run_dir = _run_paths(repo_root, pet_id)
    return _load_state_at(run_dir, pet_id, display_name)


def adopt_canonical(
    project: PetProject,
    repo_root: Path,
    *,
    reset_generated_work: bool = False,
) -> RunState:
    repo_root, runs_root, run_dir = _run_paths(repo_root, project.pet_id)
    canonical = project.canonical_base_path
    if canonical is None:
        raise RunPreparationError("durable canonical base is missing")
    try:
        _validated_png_file(canonical)
        _validate_tree_for_adoption(run_dir)
        manifest = _read_json_document(run_dir / "imagegen-jobs.json")
        jobs = _validated_jobs_for_adoption(manifest)
        request = _read_json_document(run_dir / "pet_request.json")
        metadata = _read_json_document(run_dir / "omnipet-run.json")
        for payload in (manifest, request, metadata):
            _reject_secrets(payload)
        if _is_fully_reconciled(project, run_dir, manifest, request, metadata):
            return _load_state_at(run_dir, project.pet_id, project.display_name)
        if _has_nonbase_evidence(run_dir, jobs) and not reset_generated_work:
            raise ValueError("canonical adoption would reset generated work")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RunPreparationError):
        raise RunPreparationError("canonical adoption preflight failed") from None

    omnipet_root = runs_root.parent
    archives_root = omnipet_root / "archives"
    if archives_root.is_symlink() or (archives_root.exists() and not archives_root.is_dir()):
        raise RunPreparationError("canonical archive root is invalid")
    _validate_tree_path(repo_root, archives_root)
    archives_root.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    archive_name = f"{project.pet_id}-canonical-adoption-{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}"
    archive = archives_root / archive_name
    staging = Path(tempfile.mkdtemp(prefix=f".{project.pet_id}-adopt-", dir=runs_root))
    archive_staging = Path(tempfile.mkdtemp(prefix=f".{archive_name}-", dir=archives_root))
    backup = runs_root / f".{project.pet_id}-adopt-backup"
    archive_published = False
    try:
        _archive_safe_evidence(run_dir, archive_staging)
        _write_archive_manifest(archive_staging)
        archive_manifest_sha256 = _sha256(archive_staging / "archive-manifest.json")

        prompts = staging / "prompts"
        prompt_files = build_current_prompts(project, request)
        _write_current_prompts(prompt_files, prompts)
        prompt_manifest = {
            path: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for path, content in prompt_files.items()
        }
        references = staging / "references"
        references.mkdir()
        for index, reference in enumerate(project.reference_paths, start=1):
            _atomic_validated_copy(
                reference, references / f"reference-{index:02d}{reference.suffix.lower()}"
            )
        old_guides = run_dir / "references" / "layout-guides"
        if old_guides.is_dir():
            _copy_run_path(run_dir, old_guides, references / "layout-guides")

        digest = _sha256(canonical)
        source_name = f"canonical-approved-{digest[:12]}.png"
        generated_source = staging / "generated-sources" / source_name
        generated_source.parent.mkdir()
        decoded = staging / "decoded"
        decoded.mkdir()
        _atomic_validated_copy(canonical, generated_source)
        _atomic_validated_copy(canonical, decoded / "base.png")
        _atomic_validated_copy(canonical, references / "canonical-base.png")

        completed_at = timestamp.isoformat()
        normalized_manifest = _adopted_manifest(
            manifest, jobs, run_dir, generated_source, digest, completed_at
        )
        _atomic_write_json(staging / "imagegen-jobs.json", normalized_manifest)
        _upgrade_manifest_canvas(staging / "imagegen-jobs.json")
        normalized_request = dict(request)
        normalized_request.update({
            "pet_notes": _current_pet_notes(project),
            "current_canonical": {
                "project_path": str(canonical.relative_to(project.root)),
                "run_path": "references/canonical-base.png",
                "sha256": digest,
            },
            "current_reference_path": "references/canonical-base.png",
        })
        _atomic_write_json(staging / "pet_request.json", normalized_request)
        normalized_metadata = dict(metadata)
        normalized_metadata.update({
            "schema_version": 1,
            "pet_id": project.pet_id,
            "canonical_base": {
                "project_path": str(canonical.relative_to(project.root)),
                "run_path": "references/canonical-base.png",
                "sha256": digest,
            },
            "prompt_manifest": prompt_manifest,
        })
        _atomic_write_json(staging / "omnipet-run.json", normalized_metadata)
        qa = staging / "qa"
        visual_jobs = qa / "visual-jobs"
        visual_jobs.mkdir(parents=True)
        (qa / "progress.md").write_text(_progress_text(project), encoding="utf-8")
        _atomic_write_json(qa / "time-log.json", {
            "target_minutes": 30,
            "allocations": {
                "preparation": 2,
                "base": 3,
                "standard_rows": 10,
                "look_rows": 8,
                "final_qa": 5,
                "packaging": 2,
            },
            "entries": [{
                "stage": "canonical adoption",
                "started_at": completed_at,
                "ended_at": completed_at,
                "elapsed_minutes": 0,
                "repair_minutes": 0,
                "artifact": f".omnipet/runs/{project.pet_id}/imagegen-jobs.json",
                "decision": "Approved durable canonical adopted; nonbase work reset.",
            }],
            "checkpoints": [],
        })
        _atomic_write_json(visual_jobs / f"{generated_source.stem}.result.json", {
            "ok": True,
            "job_id": "base",
            "attempts": 0,
            "retry_used": False,
            "source_path": str(run_dir / "generated-sources" / generated_source.name),
            "prompt_file": "prompts/base-pet.md",
            "canvas": {"aspect_ratio": "1:1", "image_size": "1K"},
            "sha256": digest,
            "completed_at": completed_at,
            "adoption_decision": "approved durable canonical",
            "archive_manifest_sha256": archive_manifest_sha256,
            "archive": {
                "path": str(archive.relative_to(repo_root)),
                "policy": "safe-evidence-v2",
            },
        })
        _atomic_validated_copy(canonical, staging / "generated-sources" / "base.png")
        (qa / "candidates").mkdir(parents=True)
        _atomic_write_json(qa / "candidates" / "base.json", {
            "schema_version": 1,
            "job_id": "base",
            "source_path": "generated-sources/base.png",
            "sha256": digest,
            "canvas": {"aspect_ratio": "1:1", "image_size": "1K"},
        })
        (qa / "base").mkdir(parents=True)
        _atomic_write_json(qa / "base" / "review.json", {
            "adoption_decision": "approved durable canonical",
            "canvas": {"aspect_ratio": "1:1", "image_size": "1K"},
            "completed_at": completed_at,
            "job_id": "base",
            "ok": True,
            "sha256": digest,
        })

        _load_state_at(staging, project.pet_id, project.display_name)
        _validate_canonical_base(project, staging)
        if backup.exists() or archive.exists():
            raise RunPreparationError("canonical adoption transaction path already exists")
        os.replace(run_dir, backup)
        try:
            os.replace(staging, run_dir)
            os.replace(archive_staging, archive)
            archive_published = True
        except Exception:
            if run_dir.exists():
                shutil.rmtree(run_dir)
            os.replace(backup, run_dir)
            if archive.exists():
                shutil.rmtree(archive)
            raise
        state = _load_state_at(run_dir, project.pet_id, project.display_name)
        try:
            shutil.rmtree(backup)
        except OSError:
            pass
        return state
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if archive_staging.exists():
            shutil.rmtree(archive_staging)
        if backup.exists() and not run_dir.exists():
            os.replace(backup, run_dir)
        if archive_published and archive.exists():
            shutil.rmtree(archive)
        raise RunPreparationError("canonical adoption failed") from None


def refresh_prompts(project: PetProject, repo_root: Path) -> RunState:
    repo_root, runs_root, run_dir = _run_paths(repo_root, project.pet_id)
    request = _read_json_document(run_dir / "pet_request.json")
    metadata = _read_json_document(run_dir / "omnipet-run.json")
    analysis_path = run_dir / "qa" / "pet-analysis.md"
    if not analysis_path.is_file():
        raise RunPreparationError(
            "qa/pet-analysis.md not found — write the pet analysis before refreshing prompts"
        )
    analysis_text = analysis_path.read_text(encoding="utf-8")
    prompt_files = build_current_prompts(project, request, analysis_text=analysis_text)
    _write_current_prompts(prompt_files, run_dir / "prompts")
    prompt_manifest = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in prompt_files.items()
    }
    metadata["prompt_manifest"] = prompt_manifest
    _atomic_write_json(run_dir / "omnipet-run.json", metadata)

    manifest_path = run_dir / "imagegen-jobs.json"
    manifest = _read_json_document(manifest_path)
    jobs = manifest.get("jobs")
    if isinstance(jobs, list):
        for job in jobs:
            if not isinstance(job, dict) or "prompt_file" not in job:
                continue
            prompt_path = run_dir / job["prompt_file"]
            if not prompt_path.is_file():
                continue
            job_meta = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
            job_meta["prompt_sha256"] = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
            job["metadata"] = job_meta
        _atomic_write_json(manifest_path, manifest)

    return _load_state_at(run_dir, project.pet_id, project.display_name)


def _adopted_manifest(
    manifest: Any,
    jobs: list[dict[str, Any]],
    run_dir: Path,
    generated_source: Path,
    digest: str,
    completed_at: str,
) -> dict[str, Any]:
    payload = {key: manifest[key] for key in _MANIFEST_KEYS if key in manifest and key != "jobs"}
    payload.update({"schema_version": 1, "run_dir": str(run_dir)})
    normalized = []
    generated_fields = {
        "source_path", "completed_at", "derived_from", "mirror_decision", "metadata",
        "repair_source_paths", "adoption_decision",
    }
    for source_job in jobs:
        job = {key: source_job[key] for key in _JOB_KEYS if key in source_job}
        canvas = canvas_for_job(job["id"], job["kind"])
        job["canvas"] = {"aspect_ratio": canvas.aspect_ratio, "image_size": canvas.image_size}
        job["output_path"] = f"decoded/{job['id']}.png"
        if job["id"] == "base":
            job["prompt_file"] = "prompts/base-pet.md"
        elif job["id"] == "look-cardinals":
            job["prompt_file"] = "prompts/look-cardinals.md"
            job["repair_prompt_files"] = {
                direction: f"prompts/look-anchor-repairs/{direction}.md"
                for direction in ("000", "090", "180", "270")
            }
        else:
            job["prompt_file"] = f"prompts/rows/{job['id']}.md"
            job["retry_prompt_file"] = f"prompts/row-retries/{job['id']}.md"
        if job["id"] == "base":
            job.update({
                "status": "complete",
                "source_path": str(run_dir / "generated-sources" / generated_source.name),
                "completed_at": completed_at,
                "metadata": {"sha256": digest, "format": "PNG"},
                "adoption_decision": "approved durable canonical",
            })
            job.pop("derived_from", None)
            job.pop("mirror_decision", None)
            job.pop("repair_source_paths", None)
        else:
            job["status"] = "pending"
            for key in generated_fields:
                job.pop(key, None)
        normalized.append(job)
    payload["jobs"] = normalized
    return payload


def _validated_jobs_for_adoption(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("invalid adoption manifest")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != len(EXPECTED_JOB_IDS):
        raise ValueError("invalid adoption jobs")
    for job, job_id in zip(jobs, EXPECTED_JOB_IDS, strict=True):
        if (
            not isinstance(job, dict)
            or job.get("id") != job_id
            or not isinstance(job.get("kind"), str)
            or tuple(job.get("depends_on", ())) != EXPECTED_DEPENDENCIES[job_id]
        ):
            raise ValueError("invalid adoption job structure")
        canvas_for_job(job_id, job["kind"])
    return jobs


def _has_nonbase_evidence(run_dir: Path, jobs: list[dict[str, Any]]) -> bool:
    generated_fields = {
        "source_path", "completed_at", "derived_from", "mirror_decision",
        "repair_source_paths", "adoption_decision",
    }
    if any(
        job["status"] != "pending"
        or not generated_fields.isdisjoint(job)
        or (
            isinstance(job.get("metadata"), dict)
            and set(job["metadata"]) - {"prompt_sha256"}
        )
        for job in jobs[1:]
    ):
        return True
    decoded = run_dir / "decoded"
    if decoded.is_dir() and any(path.name != "base.png" for path in decoded.iterdir()):
        return True
    generated = run_dir / "generated-sources"
    base_source = Path(jobs[0].get("source_path", ""))
    base_candidate_source = generated / "base.png"
    if generated.is_dir() and any(
        path != base_source and path != base_candidate_source
        for path in generated.iterdir()
    ):
        return True
    visual = run_dir / "qa" / "visual-jobs"
    if visual.is_dir():
        for path in visual.iterdir():
            try:
                result = _read_json_document(path)
            except (OSError, ValueError, json.JSONDecodeError):
                return True
            if not isinstance(result, dict) or result.get("job_id") != "base":
                return True
    return False


def _validate_tree_for_adoption(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("run tree is invalid")
    canonical_root = root.resolve()
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            if path.is_symlink():
                try:
                    resolved = path.resolve(strict=False)
                except (OSError, RuntimeError) as exc:
                    raise ValueError("run tree contains an invalid symlink") from exc
                if resolved == path.absolute() or resolved.is_dir():
                    raise ValueError("run tree contains a directory symlink")
                if not resolved.is_relative_to(canonical_root):
                    raise ValueError("run tree symlink escapes its root")
            elif path.is_dir():
                pending.append(path)


def _is_fully_reconciled(
    project: PetProject,
    run_dir: Path,
    manifest: Any,
    request: Any,
    metadata: Any,
) -> bool:
    if not isinstance(manifest, dict) or not isinstance(request, dict) or not isinstance(metadata, dict):
        return False
    canonical = project.canonical_base_path
    if canonical is None:
        return False
    digest = _sha256(canonical)
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != len(EXPECTED_JOB_IDS):
        return False
    base = jobs[0]
    source_value = base.get("source_path")
    if not isinstance(source_value, str) or not Path(source_value).is_absolute():
        return False
    source = Path(source_value)
    result_path = run_dir / "qa" / "visual-jobs" / f"{source.stem}.result.json"
    expected_source = run_dir / "generated-sources" / f"canonical-approved-{digest[:12]}.png"
    if source != expected_source or not source.is_relative_to(run_dir / "generated-sources"):
        return False
    try:
        paths = (
            run_dir / "references" / "canonical-base.png",
            source,
            run_dir / "decoded" / "base.png",
        )
        if any(_sha256(path) != digest for path in paths):
            return False
        result = _read_json_document(result_path)
    except (OSError, ValueError, json.JSONDecodeError, RunPreparationError):
        return False
    expected_top_level = {
        "pet_request.json", "imagegen-jobs.json", "omnipet-run.json", "prompts",
        "references", "generated-sources", "decoded", "qa",
    }
    generated_root = run_dir / "generated-sources"
    decoded_root = run_dir / "decoded"
    canonical_metadata = {
        "project_path": str(canonical.relative_to(project.root)),
        "run_path": "references/canonical-base.png",
        "sha256": digest,
    }
    analysis_path = run_dir / "qa" / "pet-analysis.md"
    analysis_text = ""
    if analysis_path.is_file():
        analysis_text = analysis_path.read_text(encoding="utf-8")
    expected_prompts = build_current_prompts(project, request, analysis_text=analysis_text)
    expected_prompt_manifest = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in expected_prompts.items()
    }
    generated_fields = {
        "source_path", "completed_at", "derived_from", "mirror_decision",
        "repair_source_paths", "adoption_decision",
    }
    expected_canvases = [
        {"aspect_ratio": "1:1", "image_size": "1K"},
        *([{"aspect_ratio": "21:9", "image_size": "2K"}] * 12),
    ]
    expected_result_keys = {
        "ok", "job_id", "attempts", "retry_used", "source_path", "prompt_file",
        "canvas", "sha256", "completed_at", "adoption_decision", "archive",
        "archive_manifest_sha256",
    }
    expected_qa_files = {
        "progress.md", "time-log.json", f"visual-jobs/{source.stem}.result.json",
        "candidates/base.json", "base/review.json",
    }
    return (
        {path.name for path in run_dir.iterdir()} == expected_top_level
        and {path.name for path in generated_root.iterdir()} == {source.name, "base.png"}
        and {path.name for path in decoded_root.iterdir()} == {"base.png"}
        and base.get("metadata", {}).get("sha256") == digest
        and base.get("metadata", {}).get("format") == "PNG"
        and base.get("status") == "complete"
        and base.get("output_path") == "decoded/base.png"
        and base.get("prompt_file") == "prompts/base-pet.md"
        and isinstance(base.get("completed_at"), str)
        and bool(base["completed_at"])
        and base.get("adoption_decision") == "approved durable canonical"
        and all(
            job.get("status") == "pending"
            and generated_fields.isdisjoint(job)
            and (
                not isinstance(job.get("metadata"), dict)
                or set(job["metadata"]) <= {"prompt_sha256"}
            )
            for job in jobs[1:]
        )
        and [job.get("canvas") for job in jobs] == expected_canvases
        and [job.get("id") for job in jobs] == list(EXPECTED_JOB_IDS)
        and result.get("job_id") == "base"
        and set(result) == expected_result_keys
        and result.get("ok") is True
        and result.get("sha256") == digest
        and result.get("source_path") == str(source)
        and result.get("completed_at") == base.get("completed_at")
        and result.get("canvas") == {"aspect_ratio": "1:1", "image_size": "1K"}
        and result.get("prompt_file") == "prompts/base-pet.md"
        and result.get("adoption_decision") == "approved durable canonical"
        and isinstance(result.get("archive_manifest_sha256"), str)
        and len(result["archive_manifest_sha256"]) == 64
        and isinstance(result.get("archive", {}).get("path"), str)
        and result["archive"]["path"].startswith(".omnipet/archives/")
        and result["archive"].get("policy") == "safe-evidence-v2"
        and set(result["archive"]) == {"path", "policy"}
        and metadata.get("canonical_base") == canonical_metadata
        and metadata.get("prompt_manifest") == expected_prompt_manifest
        and request.get("current_canonical") == canonical_metadata
        and request.get("current_reference_path") == "references/canonical-base.png"
        and request.get("pet_notes") == _current_pet_notes(project)
        and _prompts_are_current(run_dir / "prompts", expected_prompts)
        and {
            str(path.relative_to(run_dir / "qa"))
            for path in (run_dir / "qa").rglob("*")
            if path.is_file()
        } == expected_qa_files
        and _validated_archive_provenance(
            project, run_dir, result["archive"], result["archive_manifest_sha256"]
        )
    )


def _current_pet_notes(project: PetProject) -> str:
    if project.brief_path.is_symlink() or not project.brief_path.is_file():
        raise RunPreparationError("durable pet brief is invalid")
    return " ".join(project.brief_path.read_text(encoding="utf-8").split())


_STATE_PROMPTS = {
    "idle": "Calm low-distraction resting loop: subtle breathing, tiny blink, slight head/body bob, and only quiet persona-preserving motion.",
    "running-right": "Dragging-right loop: show directional movement to the right through body and limb poses only. The character must face and travel right — rotate the body, shift the gaze direction, and lean into the movement.",
    "running-left": "Dragging-left loop: show directional movement to the left through body and limb poses only. The character must face and travel left — rotate the body, shift the gaze direction, and lean into the movement.",
    "waving": "Greeting loop: paw or limb down, raised, tilted, and returning in a friendly attention gesture.",
    "jumping": "Hover jump loop: anticipation, lift, airborne peak, descent, and settle through body height.",
    "failed": "Blocked/failed loop: slumped or deflated reaction with sad or closed eyes.",
    "waiting": "Needs-input loop: expectant asking pose for approval, help, or user input.",
    "running": "Working loop: focused active-task processing, thinking, typing, scanning, or effortful concentration; not literal foot-running, jogging, sprinting, treadmill motion, raised knees, long steps, pumping arms, or directional travel.",
    "review": "Ready-review loop: focused inspection of completed output with lean, blink, narrowed eyes, head tilt, or paw pose.",
}

_STATE_REQUIREMENTS = {
    "idle": [
        "CRITICAL: idle is the low-distraction baseline state and the first frame is also used as the reduced-motion static pet.",
        "Use only subtle idle motion: gentle breathing, a tiny blink, a slight head or body bob, a very small material sway, or another quiet motion that fits the pet persona.",
        "Keep the pet essentially in the same pose, facing direction, silhouette, markings, palette, and prop state across all frames.",
        "Idle variation must stay calm but still read as animation; do not repeat effectively identical copies across the loop.",
        "Do not show waving, walking, running, jumping, talking, working, reviewing, emotional reactions, large gestures, item interactions, or new props.",
        "Feet, base, body, or object anchor should remain planted or nearly planted.",
        "The first and last frames should be very close visually so the loop feels calm and does not pop.",
    ],
    "running-right": [
        "Show directional drag movement to the right through body, limb, and prop movement only.",
        "The character must unmistakably face and travel right — rotate the body, change the gaze direction, and shift weight rightward.",
        "The movement cadence must alternate visibly across the frames instead of repeating one nearly static stride.",
        "Vary the pose and expression between frames to convey effort, momentum, and direction.",
        "Do not draw speed lines, dust clouds, floor shadows, motion trails, or detached motion effects.",
    ],
    "running-left": [
        "Show directional drag movement to the left through body, limb, and prop movement only.",
        "The character must unmistakably face and travel left — rotate the body, change the gaze direction, and shift weight leftward.",
        "The movement cadence must alternate visibly across the frames instead of repeating one nearly static stride.",
        "Vary the pose and expression between frames to convey effort, momentum, and direction.",
        "Do not draw speed lines, dust clouds, floor shadows, motion trails, or detached motion effects.",
    ],
    "waving": [
        "Show the greeting through paw, hand, wing, or limb pose only.",
        "Vary the expression and pose between frames to convey a friendly, animated greeting.",
        "Do not draw wave marks, motion arcs, lines, sparkles, symbols, or floating effects around the gesture.",
    ],
    "jumping": [
        "Show the jump through pose and vertical body position only: anticipation, lift, airborne peak, descent, settle.",
        "Vary the expression between frames to convey effort at the peak and relief on landing.",
        "Do not draw ground shadows, contact shadows, drop shadows, oval shadows, landing marks, dust, smears, bounce pads, or motion marks under the pet.",
        "Keep the background outside the pet perfectly flat chroma key with no darker key-colored patches.",
    ],
    "failed": [
        "Show failure through slumped pose, drooping ears/limbs, closed or sad eyes, and lower body position.",
        "Vary the expression between frames to show the emotional shift from disappointment to recovery.",
        "Tears, small smoke puffs, or tiny stars are allowed only if attached to or overlapping the pet silhouette and kept inside the same frame slot.",
        "Do not draw red X marks, floating symbols, detached stars, separated smoke clouds, falling tear drops, dust, or other loose effects.",
    ],
    "waiting": [
        "Show that Codex needs approval, help, or user input through an expectant asking pose.",
        "Vary the expression and pose between frames to convey growing anticipation.",
        "Keep the motion patient and readable, without turning it into ordinary idle or review.",
    ],
    "running": [
        "Show the pet actively working or processing, as if running a task: focused posture, busy hands or paws, purposeful bobbing, thinking motion, tool or prop motion only if already part of the pet identity, or other non-locomotion activity.",
        "Vary the expression and pose between frames to convey concentration and effort.",
        "Do not show literal foot-running, jogging, sprinting, treadmill motion, raised knees, long steps, pumping arms, directional travel, speed lines, dust clouds, floor shadows, motion trails, or detached motion effects.",
    ],
    "review": [
        "Show review through lean, blink, narrowed eyes, head tilt, or paw/hand position.",
        "Vary the expression and pose between frames to convey focused inspection.",
        "Do not add magnifying glasses, papers, code, UI, punctuation, symbols, or other new props unless they already exist in the base pet identity.",
    ],
}

_DEFAULT_ROW_FRAMES = {
    "idle": 6,
    "running-right": 8,
    "running-left": 8,
    "waving": 4,
    "jumping": 5,
    "failed": 8,
    "waiting": 6,
    "running": 6,
    "review": 6,
}

_STANDARD_ROW_STATES = (
    "idle", "running-right", "running-left", "waving", "jumping",
    "failed", "waiting", "running", "review",
)

_LOOK_ROW_ACTIONS = {
    "look-row-9": "Render the first eight clockwise look directions.",
    "look-row-10": "Render the final eight clockwise look directions.",
}

_LOOK_ROW_DIRECTIONS = {
    "look-row-9": ["000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5"],
    "look-row-10": ["180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5"],
}

_LOOK_ROW_NUMBERS = {
    "look-row-9": 9,
    "look-row-10": 10,
}


def _identity_contract(project: PetProject) -> str:
    return (
        f"Keep {project.display_name}'s core identity — palette, costume, accessories, props, "
        "species, and proportions — consistent in every image. Preserve the durable brief and "
        "reference images. Vary pose, expression, body orientation, facing direction, and "
        "posture freely between frames to convey the action; the character is a living, "
        "animated being, not a static statue."
    )


def _row_prompt_body(
    project: PetProject,
    request: Mapping[str, Any],
    state: str,
) -> str:
    pet_notes = request.get("pet_notes") or _current_pet_notes(project)
    style_contract = request.get("style_contract", "")
    chroma = request.get("chroma_key") or {}
    chroma_hex = chroma.get("hex", "#00FFFF")
    chroma_name = chroma.get("name", "cyan")
    contract = _identity_contract(project)
    state_prompt = _STATE_PROMPTS.get(state, "")
    state_reqs = _STATE_REQUIREMENTS.get(state, [])
    state_reqs_text = "\n".join(f"- {line}" for line in state_reqs)
    frames = _DEFAULT_ROW_FRAMES.get(state, 6)
    for row_spec in request.get("rows") or []:
        if row_spec.get("state") == state:
            frames = row_spec.get("frames", frames)
            break
    return (
        f"# {state}\n\n"
        f"Create one horizontal animation strip for Codex pet `{project.pet_id}`, state `{state}`.\n\n"
        "Use the attached canonical base for identity. Use the attached layout guide only for "
        "slot count, spacing, centering, and padding; do not draw the guide.\n\n"
        f"Output exactly {frames} full-body frames in one left-to-right row on flat pure "
        f"{chroma_name} {chroma_hex}. Treat the row as {frames} invisible equal-width slots: "
        "one centered complete pose per slot, evenly spaced, with no overlap, clipping, empty "
        "slots, labels, or borders. Keep a clear chroma-only gap between neighboring poses so "
        "each complete pose can be detected as a separate group without cutting through "
        "foreground; never let two poses touch or merge into one connected silhouette.\n\n"
        f"Identity: {contract} Same pet in every frame: {pet_notes}\n"
        f"Style: {style_contract}\n"
        "Animation continuity: keep apparent pet scale and baseline stable within the row "
        "unless the state itself intentionally changes vertical position, such as `jumping`. "
        "Move the pose within the slot instead of redrawing the pet larger or smaller frame to frame.\n"
        "Composition: the character must fill at least 75% of the slot height. Do not leave "
        "large empty space above, below, or around the character. The character should be "
        "large, prominent, and clearly readable at small sizes — not small or distant.\n\n"
        f"State action: {state_prompt}\n\n"
        f"State requirements:\n{state_reqs_text}\n\n"
        "Clean extraction: crisp opaque edges, safe padding, no scenery, text, guide marks, "
        "checkerboard, shadows, glows, motion blur, speed lines, dust, detached effects, stray "
        "pixels, or chroma-key colors inside the pet."
    )


def _look_cardinal_prompt_body(
    project: PetProject,
    request: Mapping[str, Any],
) -> str:
    contract = _identity_contract(project)
    chroma = request.get("chroma_key") or {}
    chroma_hex = chroma.get("hex", "#00FFFF")
    chroma_name = chroma.get("name", "cyan")
    return (
        f"Create one horizontal four-cardinal anchor strip for Codex pet `{project.pet_id}`.\n\n"
        f"Use the attached canonical base, completed standard contact sheet, and layout guide "
        f"for exact identity, style, scale, baseline, face construction, materials, palette, "
        f"markings, props, and spacing. Read `qa/look-mechanics.md` and use the pet's natural "
        f"gaze mechanism.\n\n"
        f"Identity: {contract}\n\n"
        "Output exactly four centered complete full-body poses in this exact left-to-right "
        "order: `000 up`, `090 screen-right`, `180 down`, `270 screen-left`. Screen-left and "
        "screen-right always mean the viewer's image edges, never the character's own left or right.\n\n"
        "For `000`, keep the face broadly frontal and point the eyes and natural head mechanism "
        "toward the TOP edge. For `090`, put the nose tip, pupils, face surface, or natural "
        "aiming feature on the screen-right side of the head center. For `180`, keep the face "
        "broadly frontal and point toward the BOTTOM edge. For `270`, apply the inverse "
        "screen-left landmark rule. Every cardinal must be unmistakable without labels.\n\n"
        f"Place one pose in each invisible equal-width slot on a flat pure {chroma_name} {chroma_hex} "
        "background with generous padding. Keep scale, feet/base, lower body, and registration "
        "consistent across all four slots.\n\n"
        "Do not rotate, skew, or tilt the whole sprite to fake gaze. Do not add replacement "
        "eyes, labels, degree text, arrows, boxes, guide marks, shadows, scenery, detached "
        "effects, or chroma-key colors inside the pet."
    )


def _look_row_prompt_body(
    project: PetProject,
    request: Mapping[str, Any],
    state: str,
) -> str:
    contract = _identity_contract(project)
    chroma = request.get("chroma_key") or {}
    chroma_hex = chroma.get("hex", "#00FFFF")
    chroma_name = chroma.get("name", "cyan")
    row = _LOOK_ROW_NUMBERS[state]
    directions = _LOOK_ROW_DIRECTIONS[state]
    direction_list = ", ".join(directions)
    reference_instruction = (
        "The approved cardinal strip is authoritative for the up, screen-right, down, "
        "and screen-left pose families. Interpolate the intermediate directions as "
        "even 22.5-degree steps between those anchors."
        if row == 9
        else "The approved cardinal strip and completed coherent row 9 are authoritative. "
        "Use the cardinals for direction meaning and row 9 for cross-row identity, scale, "
        "registration, and continuity."
    )
    return (
        f"Create one horizontal look-direction strip for Codex pet `{project.pet_id}`, atlas row {row}.\n\n"
        f"Use the attached canonical base, completed standard contact sheet, layout guide, "
        f"and approved four-cardinal strip for identity, scale, registration, spacing, "
        f"direction semantics, and cross-row continuity. Read `qa/look-mechanics.md` and "
        f"follow its pet-specific movement and eye/prop mechanics. {reference_instruction}\n\n"
        f"Identity: {contract}\n\n"
        "COHERENT SYNTHESIS LOCK: produce one unified eight-pose row. Do not paste, tile, "
        "or independently restyle individual cells. Every final cell must be drawn together "
        "with the same face construction, body proportions, line/render quality, lighting, "
        "materials, scale, baseline, and registration.\n\n"
        f"Output exactly 8 complete full-body frames in this exact left-to-right order: "
        f"{direction_list}. Degrees are clockwise: 000 is up, 090 right, 180 down, and 270 "
        f"left. Neutral/front is not part of this row.\n\n"
        f"{look_row_axis_contract(row)}\n\n"
        f"{look_row_screen_coordinate_contract(row)}\n\n"
        f"{look_row_layout_contract()}\n\n"
        f"Place one centered pose in each invisible equal-width slot on flat pure "
        f"{chroma_name} {chroma_hex}. Change only the natural parts needed to express gaze: "
        "eyes, eyelids, head, face, neck, upper body, appendages, and constrained prop "
        "follow-through. Keep identity, silhouette, materials, palette, markings, and props "
        "consistent.\n\n"
        f"{look_row_boundary_contract(row)}\n\n"
        f"{look_row_pre_return_check(row)}\n\n"
        "Do not rotate, skew, or tilt the whole sprite to fake gaze. Do not add "
        "replacement/googly eyes, labels, degree text, arrows, clocks, grids, shadows, "
        "glows, scenery, detached effects, or chroma-key colors inside the pet."
    )


def _analysis_section(analysis_text: str) -> str:
    if not analysis_text.strip():
        return ""
    return (
        "\n\nPET-SPECIFIC DESIGN NOTES (authoritative — overrides generic guidance "
        "where they conflict):\n"
        f"{analysis_text.strip()}\n"
    )


def _design_context_section(design_context: Mapping[str, Any] | None) -> str:
    if not design_context:
        return ""
    contract = design_context.get("contract")
    storyboard = design_context.get("storyboard")
    anchors = design_context.get("pose_anchors", ())
    if not isinstance(contract, Mapping) or not isinstance(storyboard, Mapping):
        raise ValueError("design context is invalid")
    return (
        "\n\nApproved Design Pack (authoritative):\n"
        f"Design revision: {contract.get('design_revision')}\n"
        f"Character construction: {json.dumps(contract.get('character_construction'), sort_keys=True)}\n"
        f"Asymmetry contract: {json.dumps(contract.get('asymmetries'), sort_keys=True)}\n"
        f"State grammar: {json.dumps(contract.get('state_grammar'), sort_keys=True)}\n"
        f"Prohibited strategies: {json.dumps(contract.get('prohibited_strategies'), sort_keys=True)}\n"
        f"Storyboard: {json.dumps(storyboard.get('states'), sort_keys=True)}\n"
        f"Approved pose anchors: {', '.join(str(item) for item in anchors)}\n"
        "Use attached approved pose anchors for identity, pose, silhouette, and view evidence.\n"
    )


def build_current_prompts(
    project: PetProject,
    request: Mapping[str, Any],
    *,
    analysis_text: str = "",
    design_context: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    contract = _identity_contract(project)
    pet_notes = request.get("pet_notes") or _current_pet_notes(project)
    analysis = _analysis_section(analysis_text)
    design = _design_context_section(design_context)
    prompts = {
        "base-pet.md": f"# {project.display_name} Base\n\n{contract}\n\nUse the durable brief: {pet_notes}\n",
    }
    for state in _STANDARD_ROW_STATES:
        body = _row_prompt_body(project, request, state) + analysis + design
        prompts[f"rows/{state}.md"] = body
        prompts[f"row-retries/{state}.md"] = (
            body + "\nRetry by correcting the complete coherent strip without changing identity.\n"
        )
    cardinal_body = _look_cardinal_prompt_body(project, request) + analysis
    prompts["look-cardinals.md"] = cardinal_body
    prompts["look-cardinals-retry.md"] = (
        cardinal_body + "\nRetry by correcting the complete four-cardinal strip without changing identity.\n"
    )
    for state in _LOOK_ROW_ACTIONS:
        body = _look_row_prompt_body(project, request, state) + analysis
        prompts[f"rows/{state}.md"] = body
        prompts[f"row-retries/{state}.md"] = (
            body + "\nRetry by correcting the complete coherent strip without changing identity.\n"
        )
    for direction in ("000", "090", "180", "270"):
        prompts[f"look-anchor-repairs/{direction}.md"] = (
            f"# Repair {direction}\n\n{contract}\n\nRepair only the {direction} look anchor coherently.\n"
        )
    return prompts


def _write_current_prompts(prompts: Mapping[str, str], prompt_root: Path) -> None:
    for relative, content in prompts.items():
        path = prompt_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _prompts_are_current(prompt_root: Path, expected: Mapping[str, str]) -> bool:
    if prompt_root.is_symlink() or not prompt_root.is_dir():
        return False
    actual = {str(path.relative_to(prompt_root)) for path in prompt_root.rglob("*.md")}
    if actual != set(expected):
        return False
    return all((prompt_root / path).read_text(encoding="utf-8") == content for path, content in expected.items())


_ARCHIVE_TOP_LEVEL = {
    "generated-sources", "decoded", "qa", "frames", "final", "previews",
    "imagegen-jobs.json", "pet_request.json", "omnipet-run.json",
}
_ARCHIVE_DENIED_NAMES = {
    "tmp", "temp", "cache", "provider-cache", ".cache", "raw-response.json",
    "raw_response.json", "provider-response.json", "credentials", "secrets",
}
_TEXT_EXTENSIONS = {".json", ".txt", ".md", ".log"}
_IMAGE_FORMATS = {".png": "PNG", ".gif": "GIF", ".webp": "WEBP"}


def _archive_safe_evidence(run_dir: Path, archive_dir: Path) -> None:
    for name in _ARCHIVE_TOP_LEVEL:
        source = run_dir / name
        if source.exists():
            _archive_safe_path(run_dir, source, archive_dir / name)


def _archive_safe_path(root: Path, source: Path, destination: Path) -> None:
    lowered = source.name.lower()
    if lowered in _ARCHIVE_DENIED_NAMES or "provider-cache" in lowered:
        return
    if source.is_symlink():
        resolved = source.resolve()
        if source.is_dir() or not resolved.is_relative_to(root.resolve()):
            raise RunPreparationError("archive source contains an unsafe symlink")
        source = resolved
    if source.is_dir():
        children = []
        for child in source.iterdir():
            child_lower = child.name.lower()
            if child_lower in _ARCHIVE_DENIED_NAMES or "provider-cache" in child_lower:
                continue
            children.append(child)
        for child in children:
            _archive_safe_path(root, child, destination / child.name)
        return
    if not source.is_file() or not _archive_artifact_is_safe(source):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _archive_artifact_is_safe(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_FORMATS:
        try:
            with Image.open(path) as image:
                if image.format != _IMAGE_FORMATS[suffix]:
                    return False
                image.verify()
            return True
        except (OSError, SyntaxError, UnidentifiedImageError):
            return False
    if suffix not in _TEXT_EXTENSIONS:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if contains_credential_like_text(text):
        return False
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
            _reject_secrets(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if _contains_raw_provider_response(payload):
            return False
    return True


def _write_archive_manifest(archive_dir: Path) -> None:
    files = [
        {"path": str(path.relative_to(archive_dir)), "sha256": _sha256(path)}
        for path in sorted(archive_dir.rglob("*"))
        if path.is_file() and path.name != "archive-manifest.json"
    ]
    _atomic_write_json(
        archive_dir / "archive-manifest.json",
        {"schema_version": 1, "policy": "safe-evidence-v2", "files": files},
    )


def _validated_archive_provenance(
    project: PetProject, run_dir: Path, reference: Any, expected_manifest_sha256: str
) -> bool:
    if (
        not isinstance(reference, dict)
        or set(reference) != {"path", "policy"}
        or reference.get("policy") != "safe-evidence-v2"
        or not isinstance(reference.get("path"), str)
    ):
        return False
    relative = Path(reference["path"])
    if (
        relative.is_absolute()
        or len(relative.parts) != 3
        or relative.parts[:2] != (".omnipet", "archives")
        or not relative.name.startswith(f"{project.pet_id}-canonical-adoption-")
    ):
        return False
    repo_root = run_dir.parents[2]
    archive = repo_root / relative
    expected_parent = repo_root / ".omnipet" / "archives"
    if archive.parent != expected_parent or archive.is_symlink() or not archive.is_dir():
        return False
    try:
        manifest_path = archive / "archive-manifest.json"
        if _sha256(manifest_path) != expected_manifest_sha256:
            return False
        manifest = _read_json_document(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError, RunPreparationError):
        return False
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "policy", "files"}
        or manifest.get("schema_version") != 1
        or manifest.get("policy") != "safe-evidence-v2"
        or not isinstance(manifest.get("files"), list)
    ):
        return False
    listed: dict[str, str] = {}
    expected_directories: set[str] = set()
    for item in manifest["files"]:
        relative_path = Path(item.get("path", "")) if isinstance(item, dict) else Path()
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("sha256"), str)
            or item["path"] in listed
            or relative_path.is_absolute()
            or not relative_path.parts
            or ".." in relative_path.parts
        ):
            return False
        listed[item["path"]] = item["sha256"]
        parent = relative_path.parent
        while parent != Path("."):
            expected_directories.add(str(parent))
            parent = parent.parent
    walked = _walk_archive_without_links(archive)
    if walked is None:
        return False
    actual, actual_directories = walked
    return (
        set(listed) == set(actual)
        and expected_directories == actual_directories
        and all(
            _archive_artifact_is_safe(path)
            and _sha256(path) == listed[relative_path]
            for relative_path, path in actual.items()
        )
    )


def _walk_archive_without_links(
    archive: Path,
) -> tuple[dict[str, Path], set[str]] | None:
    files: dict[str, Path] = {}
    directories: set[str] = set()
    pending = [archive]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_symlink():
                        return None
                    relative = str(path.relative_to(archive))
                    if entry.is_dir(follow_symlinks=False):
                        directories.add(relative)
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        if path.name != "archive-manifest.json":
                            files[relative] = path
                    else:
                        return None
    except OSError:
        return None
    return files, directories


def _contains_raw_provider_response(value: Any) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = key.lower().replace("-", "_")
                if normalized in {"raw_response", "provider_response", "raw_provider_response"}:
                    return True
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _validated_png_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("canonical is not a regular file")
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError("canonical is not PNG")
            image.verify()
    except (OSError, SyntaxError, UnidentifiedImageError):
        raise ValueError("canonical is not valid PNG") from None


def _read_json_document(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("run metadata is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def _reject_secrets(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("run metadata key is invalid")
                if is_credential_like_key(key):
                    raise ValueError("run metadata contains a secret")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)


def _progress_text(project: PetProject) -> str:
    return (
        f"# {project.display_name} Pet Progress\n\n"
        f"- [x] **Getting {project.display_name} ready**\n"
        f"- [x] **Imagining {project.display_name}'s main look**\n"
        f"- [ ] **Picturing {project.display_name}'s poses** (active)\n"
        f"- [ ] **Hatching {project.display_name}**\n"
    )


def _copy_run_path(root: Path, source: Path, destination: Path) -> None:
    if source.is_symlink():
        resolved = source.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise RunPreparationError("run contains an escaping symlink")
        source = resolved
    else:
        _validate_tree_path(root, source)
    if source.is_dir():
        destination.mkdir(parents=True)
        for child in source.iterdir():
            _copy_run_path(root, child, destination / child.name)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    else:
        raise RunPreparationError("run contains an invalid path")


def _load_state_at(run_dir: Path, pet_id: str, display_name: str | None) -> RunState:
    _validate_tree_path(run_dir.parent, run_dir)
    manifest_path = run_dir / "imagegen-jobs.json"
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("invalid run manifest path")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        jobs = _validated_jobs(data)
        _validate_completed_jobs(run_dir, jobs, data.get("run_dir"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RunPreparationError("run state is invalid") from None

    complete_ids = {job["id"] for job in jobs if job["status"] == "complete"}
    if "base" not in complete_ids:
        actionable_ids = {"base"}
    elif not {"idle", "running-right"}.issubset(complete_ids):
        actionable_ids = {"idle", "running-right"}
    elif not set(STANDARD_JOB_IDS).issubset(complete_ids):
        actionable_ids = set(STANDARD_JOB_IDS[2:])
    elif "look-cardinals" not in complete_ids:
        actionable_ids = {"look-cardinals"}
    elif "look-row-9" not in complete_ids:
        actionable_ids = {"look-row-9"}
    elif "look-row-10" not in complete_ids:
        actionable_ids = {"look-row-10"}
    else:
        actionable_ids = set()
    ready = sum(
        job["id"] in actionable_ids
        and job["status"] == "pending"
        and all(dependency in complete_ids for dependency in job["depends_on"])
        for job in jobs
    )
    status_counts = {
        status: sum(job["status"] == status for job in jobs)
        for status in ("complete", "running", "failed")
    }
    counts = {
        "total": len(jobs),
        "complete": status_counts["complete"],
        "ready": ready,
        "pending": sum(job["status"] == "pending" for job in jobs) - ready,
        "running": status_counts["running"],
        "failed": status_counts["failed"],
    }
    name = display_name or pet_id
    frontier = 3 if all(job_id in complete_ids for job_id in EXPECTED_JOB_IDS) else (
        2 if "base" in complete_ids else 1
    )
    stage_names = (
        f"Getting {name} ready",
        f"Imagining {name}'s main look",
        f"Picturing {name}'s poses",
        f"Hatching {name}",
    )
    stages = tuple(
        RunStage(
            stage_name,
            "complete" if index < frontier else ("active" if index == frontier else "pending"),
        )
        for index, stage_name in enumerate(stage_names)
    )
    return RunState(
        pet_id=pet_id,
        run_dir=run_dir,
        job_ids=tuple(job["id"] for job in jobs),
        counts=counts,
        stages=stages,
    )


def _validated_jobs(data: Any, *, allow_missing_canvas: bool = False) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("invalid run manifest")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != len(EXPECTED_JOB_IDS):
        raise ValueError("invalid run jobs")
    ids: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("invalid run job")
        job_id = job.get("id")
        status = job.get("status")
        dependencies = job.get("depends_on")
        if (
            not isinstance(job_id, str)
            or status not in {"pending", "running", "complete", "failed"}
            or not isinstance(dependencies, list)
            or not all(isinstance(item, str) for item in dependencies)
        ):
            raise ValueError("invalid run job")
        if tuple(dependencies) != EXPECTED_DEPENDENCIES.get(job_id):
            raise ValueError("unexpected run dependencies")
        if allow_missing_canvas and "canvas" not in job:
            canvas_for_job(job_id, job.get("kind"))
        else:
            validate_job_canvas(job)
        ids.append(job_id)
    if tuple(ids) != EXPECTED_JOB_IDS or len(set(ids)) != len(ids):
        raise ValueError("unexpected run jobs")
    if any(dependency not in set(ids) for job in jobs for dependency in job["depends_on"]):
        raise ValueError("unknown run dependency")
    statuses = {job["id"]: job["status"] for job in jobs}
    for job in jobs:
        if job["status"] in {"running", "failed", "complete"} and any(
            statuses[dependency] != "complete" for dependency in job["depends_on"]
        ):
            raise ValueError("run job status violates dependency closure")
    return jobs


def _upgrade_manifest_canvas(manifest_path: Path) -> None:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("invalid run manifest path")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if isinstance(jobs, list):
        for job in jobs:
            if isinstance(job, dict) and job.get("id") == "look-cardinals" and job.get("kind") == "look-cardinal-strip":
                job["kind"] = "look-cardinals"
    jobs = _validated_jobs(data, allow_missing_canvas=True)
    for job in jobs:
        if "canvas" not in job:
            canvas = canvas_for_job(job["id"], job["kind"])
            job["canvas"] = {
                "aspect_ratio": canvas.aspect_ratio,
                "image_size": canvas.image_size,
            }
        prompt = manifest_path.parent / job["prompt_file"]
        if prompt.is_symlink() or not prompt.is_file() or not prompt.resolve().is_relative_to(manifest_path.parent):
            raise ValueError("invalid job prompt")
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        metadata["prompt_sha256"] = hashlib.sha256(prompt.read_bytes()).hexdigest()
        job["metadata"] = metadata
    _atomic_write_json(manifest_path, data)


def _validate_completed_jobs(
    run_dir: Path, jobs: list[dict[str, Any]], declared_run_dir: Any
) -> None:
    jobs_by_id = {job["id"]: job for job in jobs}
    for job in jobs:
        if job["status"] != "complete":
            continue
        job_id = job["id"]
        output_rel = job.get("output_path")
        completed_at = job.get("completed_at")
        if (
            not isinstance(output_rel, str)
            or Path(output_rel).is_absolute()
            or Path(output_rel).parts != ("decoded", f"{job_id}.png")
            or not isinstance(completed_at, str)
            or not completed_at
        ):
            raise ValueError("completed run job metadata is invalid")
        output = _validated_png_under(run_dir / "decoded", run_dir / output_rel)

        if job_id == "running-left" and job.get("derived_from") is not None:
            right = jobs_by_id["running-right"]
            policy = job.get("mirror_policy")
            decision = job.get("mirror_decision")
            metadata = job.get("metadata")
            if (
                job.get("source_path") != "decoded/running-right.png"
                or job.get("derived_from") != "running-right"
                or right["status"] != "complete"
                or not isinstance(policy, dict)
                or policy.get("may_derive") is not True
                or policy.get("may_derive_from") != "running-right"
                or policy.get("derivation") != "framewise-horizontal-mirror-preserving-order"
                or policy.get("requires_explicit_approval") is not True
                or not isinstance(decision, dict)
                or decision.get("approved") is not True
                or decision.get("transform") != "framewise-horizontal-mirror-preserving-order"
                or not isinstance(decision.get("note"), str)
                or not decision["note"]
                or decision.get("approved_at") != completed_at
                or not isinstance(metadata, dict)
                or metadata.get("format") != "PNG"
                or metadata.get("mode") != "RGBA"
                or not isinstance(metadata.get("width"), int)
                or metadata["width"] <= 0
                or not isinstance(metadata.get("height"), int)
                or metadata["height"] <= 0
            ):
                raise ValueError("completed mirror provenance is invalid")
            source = _validated_png_under(run_dir / "decoded", run_dir / job["source_path"])
            if source != run_dir / right["output_path"]:
                raise ValueError("completed mirror source is invalid")
            _validate_running_left_derivation(source, output, metadata)
        else:
            if job.get("derived_from") is not None or job.get("mirror_decision") is not None:
                raise ValueError("generated job has derivation metadata")
            source_value = job.get("source_path")
            if not isinstance(source_value, str):
                raise ValueError("completed job source is missing")
            source_path = Path(source_value)
            if not source_path.is_absolute():
                raise ValueError("completed job source is not absolute")
            if not source_path.is_relative_to(run_dir):
                if not isinstance(declared_run_dir, str) or not Path(declared_run_dir).is_absolute():
                    raise ValueError("completed job source is outside the run")
                try:
                    source_path = run_dir / source_path.relative_to(Path(declared_run_dir))
                except ValueError:
                    raise ValueError("completed job source is outside the run") from None
            source = _validated_png_under(run_dir / "generated-sources", source_path)
            if _sha256(source) != _sha256(output):
                raise ValueError("completed output does not match source")


def _validated_png_under(root: Path, path: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("completed artifact root is unsafe")
    canonical_root = root.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("completed artifact escapes its root") from None
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("completed artifact contains a symlink")
    if path.is_symlink() or not path.is_file():
        raise ValueError("completed artifact is not a regular file")
    resolved = path.resolve()
    if not resolved.is_relative_to(canonical_root):
        raise ValueError("completed artifact escapes its root")
    try:
        with Image.open(resolved) as image:
            if image.format != "PNG":
                raise ValueError("completed artifact is not PNG")
            image.verify()
    except (OSError, SyntaxError, UnidentifiedImageError):
        raise ValueError("completed artifact is not valid PNG") from None
    return resolved


def _validate_running_left_derivation(
    right_path: Path, left_path: Path, metadata: dict[str, Any]
) -> None:
    try:
        with Image.open(right_path) as right_image, Image.open(left_path) as left_image:
            right = right_image.convert("RGBA")
            left = left_image.copy()
            actual_metadata = {
                "width": left_image.width,
                "height": left_image.height,
                "mode": left_image.mode,
                "format": left_image.format,
            }
    except (OSError, SyntaxError, UnidentifiedImageError):
        raise ValueError("completed mirror images are invalid") from None
    if actual_metadata != metadata or left.mode != "RGBA" or left.size != right.size:
        raise ValueError("completed mirror metadata is invalid")

    expected = Image.new("RGBA", right.size, (0, 0, 0, 0))
    slot_width = right.width / 8
    for index in range(8):
        slot_left = round(index * slot_width)
        slot_right = round((index + 1) * slot_width)
        if slot_right <= slot_left:
            raise ValueError("completed mirror strip geometry is invalid")
        expected.alpha_composite(
            ImageOps.mirror(right.crop((slot_left, 0, slot_right, right.height))),
            (slot_left, 0),
        )
    if left.tobytes() != expected.tobytes():
        raise ValueError("completed mirror output does not match its derivation")


def _reference_metadata(project: PetProject, run_dir: Path) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for index, reference in enumerate(project.references, start=1):
        suffix = reference.path.suffix.lower() or ".png"
        copied = run_dir / "references" / f"reference-{index:02d}{suffix}"
        _validate_tree_path(run_dir, copied)
        if copied.is_symlink() or not copied.is_file():
            raise RunPreparationError("prepared reference is missing")
        source_hash = _sha256(reference.path)
        if _sha256(copied) != source_hash:
            raise RunPreparationError("prepared reference does not match")
        references.append(
            {
                "project_path": str(reference.path.relative_to(project.root)),
                "role": reference.role,
                "run_path": str(copied.relative_to(run_dir)),
                "sha256": source_hash,
            }
        )
    return references


def _validate_references(project: PetProject, run_dir: Path) -> None:
    metadata_path = run_dir / "omnipet-run.json"
    _validate_tree_path(run_dir, metadata_path)
    try:
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise ValueError("missing run metadata")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected = _reference_metadata(project, run_dir)
        if (
            metadata.get("schema_version") != 1
            or metadata.get("pet_id") != project.pet_id
            or metadata.get("references") != expected
        ):
            raise ValueError("run reference mapping changed")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RunPreparationError("run references are invalid") from None


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RunPreparationError("reference is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(destination: Path, payload: Mapping[str, Any]) -> None:
    _validate_tree_path(destination.parent, destination)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _run_paths(repo_root: Path, pet_id: str) -> tuple[Path, Path, Path]:
    supplied_root = Path(repo_root).absolute()
    if supplied_root.is_symlink() or not supplied_root.is_dir():
        raise RunPreparationError("invalid repository root")
    if Path(pet_id).is_absolute() or len(Path(pet_id).parts) != 1 or pet_id in {".", ".."}:
        raise RunPreparationError("invalid pet id")
    canonical_root = supplied_root.resolve()
    runs_root = canonical_root / ".omnipet" / "runs"
    run_dir = runs_root / pet_id
    for path in (canonical_root / ".omnipet", runs_root, run_dir):
        if path.is_symlink():
            raise RunPreparationError("run path contains a symlink")
        if path.exists() and not path.is_dir():
            raise RunPreparationError("run path is not a directory")
    _validate_tree_path(canonical_root, run_dir)
    return canonical_root, runs_root, run_dir


def _validate_tree_path(root: Path, path: Path) -> None:
    canonical_root = root.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise RunPreparationError("run path escapes its root") from None
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RunPreparationError("run path contains a symlink")
    if not path.resolve(strict=False).is_relative_to(canonical_root):
        raise RunPreparationError("run path escapes its root")


def _create_runtime_parents(repo_root: Path, runs_root: Path) -> None:
    current = repo_root
    for name in (".omnipet", "runs"):
        current /= name
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise RunPreparationError("invalid runtime directory")
        current.mkdir(exist_ok=True)
    if current != runs_root:
        raise RunPreparationError("invalid runtime root")


def _validate_canonical_base(project: PetProject, run_dir: Path) -> None:
    if project.canonical_base_path is None:
        return
    destination = run_dir / "references" / "canonical-base.png"
    _validate_tree_path(run_dir, destination)
    if destination.is_symlink() or not destination.is_file():
        raise RunPreparationError("run canonical base is missing")
    if destination.read_bytes() != project.canonical_base_path.read_bytes():
        raise RunPreparationError("run canonical base does not match")


def _atomic_validated_copy(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RunPreparationError("invalid durable canonical base")
    _validate_tree_path(destination.parent.parent, destination)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".canonical-base-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        shutil.copyfile(source, temporary)
        if temporary.read_bytes() != source.read_bytes():
            raise RunPreparationError("canonical base copy failed validation")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_failed_run(runs_root: Path, run_dir: Path) -> None:
    try:
        _validate_tree_path(runs_root, run_dir)
    except RunPreparationError:
        return
    if run_dir.is_symlink():
        run_dir.unlink(missing_ok=True)
    elif run_dir.exists():
        shutil.rmtree(run_dir)
