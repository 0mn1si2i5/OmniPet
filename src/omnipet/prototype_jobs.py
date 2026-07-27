from __future__ import annotations

import fcntl
import base64
import copy
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from omnipet.actions import ActionError, validate_action_request_unlocked
from omnipet.design_contracts import validate_design_documents
from omnipet.generation import GroundingImage, ImageRequest
from omnipet.workflow import _fsync_directory, _validate_lock_identity, _workflow_lock


_JOB_KEYS = {
    "id", "pose_id", "kind", "status", "depends_on", "design_revision",
    "prototype_plan_sha256", "prompt_file", "input_images", "output_path",
    "generation_method", "metadata",
}
_STANDARD_IDS = {
    "base", "idle", "running-right", "running-left", "waving", "jumping",
    "failed", "waiting", "running", "review", "look-cardinals", "look-row-9",
    "look-row-10",
}
_DESIGN_ARTIFACTS = (
    "omnipet-run.json", "design/intake.json", "design/design-contract.json",
    "design/design-rationale.md", "design/state-storyboard.json",
    "design/prototype-plan.json", "design/look-mechanics.json",
)
_FAILURE_JOURNAL = "prototype-failure-publication.json"
_COMPLETION_JOURNAL = "prototype-completion-publication.json"


@dataclass
class _PinnedGeneration:
    identities: tuple[tuple[Path, int, int], ...]
    descriptors: dict[str, int]

    def close(self) -> None:
        for descriptor in self.descriptors.values():
            os.close(descriptor)


def _identity_record(run_dir: Path, pinned: _PinnedGeneration) -> dict[str, list[int]]:
    by_path = {str(path): [device, inode] for path, device, inode in pinned.identities}
    return {
        name: by_path[str(path)]
        for name, path in _generation_paths(run_dir).items()
    }


class PrototypeJobError(RuntimeError):
    """Raised when declared prototype work cannot be executed safely."""


def build_prototype_publication(
    contract: dict[str, Any],
    plan: dict[str, Any],
    metadata: dict[str, Any],
    plan_bytes: bytes,
    design_artifacts: dict[str, str],
) -> dict[str, bytes]:
    plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    canonical = next(
        item["pose_id"] for item in plan["prototypes"]
        if item["evidence_kind"] == "animation-ready-canonical"
    )
    prompts: dict[str, bytes] = {}
    jobs = []
    for prototype in plan["prototypes"]:
        pose_id = prototype["pose_id"]
        roles = set(prototype["reference_roles"])
        references = [
            {"path": item["run_path"], "role": item["role"], "sha256": item["sha256"]}
            for item in metadata["references"] if item["role"] in roles
        ]
        prompt_path = f"prompts/prototypes/{pose_id}.md"
        prompt = _prototype_prompt(contract, prototype)
        prompts[prompt_path] = prompt
        jobs.append({
            "id": pose_id,
            "pose_id": pose_id,
            "kind": "prototype",
            "status": "pending",
            "depends_on": [] if pose_id == canonical else [canonical],
            "design_revision": plan["design_revision"],
            "prototype_plan_sha256": plan_hash,
            "prompt_file": prompt_path,
            "input_images": references,
            "output_path": (
                "decoded/canonical.png" if pose_id == canonical
                else f"decoded/prototypes/{pose_id}.png"
            ),
            "generation_method": prototype["generation_method"],
            "metadata": {
                "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
                "canvas": {"aspect_ratio": "1:1", "image_size": "1K"},
            },
        })
    return {
        **prompts,
        "imagegen-jobs.json": _json_bytes({
            "schema_version": 2, "design_artifacts": design_artifacts, "jobs": jobs,
        }),
    }


def prototype_job_status(run_dir: Path) -> dict[str, Any]:
    try:
        path = _validated_run_dir(run_dir)
        expected = _snapshot_generation_identities(path)
        with _generation_lock(path) as acquired:
            if not acquired:
                raise ValueError("prototype generation is active")
            _pre_pin_boundary()
            pinned = _pin_generation_paths(path, expected=expected)
            try:
                recover_prototype_jobs(path, pinned=pinned)
                _path, _manifest, jobs = _validated_state_pinned(path, pinned)
                ready = _ready_jobs(jobs)
                return {
                    "declared_ids": [job["id"] for job in jobs],
                    "ready_ids": [ready[0]["id"]] if ready else [],
                    "statuses": {job["id"]: job["status"] for job in jobs},
                    "run_dir": str(path),
                }
            finally:
                pinned.close()
    except Exception:
        raise PrototypeJobError("prototype job status is invalid") from None


def validated_prototype_manifest(run_dir: Path) -> dict[str, Any]:
    """Return a detached manifest after validating current prototype state."""
    try:
        return copy.deepcopy(_validated_state(run_dir)[1])
    except Exception:
        raise PrototypeJobError("prototype job status is invalid") from None


def generate_next_prototype(
    run_dir: Path, generator: Any, *, action_id: str | None = None,
    run_revision: str | None = None,
) -> dict[str, Any] | None:
    path = _validated_run_dir(run_dir)
    try:
        # All operations needing both locks acquire workflow before generation.
        # This excludes evidence/summary mutation through action validation,
        # provider execution, and the final manifest/workflow publication.
        with _workflow_lock(path, blocking=False) as workflow_acquired:
            if not workflow_acquired:
                return None
            expected = _snapshot_generation_identities(path)
            with _generation_lock(path) as acquired:
                if not acquired:
                    return None
                _pre_pin_boundary()
                pinned = _pin_generation_paths(path, expected=expected)
                try:
                    if action_id is not None or run_revision is not None:
                        if action_id is None or run_revision is None:
                            raise ActionError("action request is invalid")
                        validate_action_request_unlocked(
                            path, action_id, run_revision, "generate-prototype"
                        )
                    recover_prototype_jobs(path, pinned=pinned)
                    manifest, jobs = _validated_state_pinned(path, pinned)[1:]
                    ready = _ready_jobs(jobs)
                    if not ready:
                        return None
                    job = ready[0]
                    if job["generation_method"] != "generate":
                        raise ValueError("prototype generation method is invalid")
                    _begin_attempt_pinned(manifest, job, pinned)
                    request = _request(path, job, pinned)
                    generated = generator.edit(request)
                    _verify_generation_paths(pinned)
                    source = Path(generated.path)
                    if source != request.destination or source.is_symlink():
                        raise ValueError("prototype source is invalid")
                    content = _read_at(pinned.descriptors["generated"], f"{job['id']}.png")
                    _validate_png_bytes(content)
                    if getattr(generated, "sha256", None) != hashlib.sha256(content).hexdigest():
                        raise ValueError("prototype source hash is invalid")
                    job["status"] = "complete"
                    job["metadata"].update({
                        "source_path": source.relative_to(path).as_posix(),
                        "source_sha256": hashlib.sha256(content).hexdigest(),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "principal": _principal(generated),
                    })
                    _publish_completion(path, manifest, job, content, pinned=pinned)
                    return {"job_id": job["id"], "status": "complete"}
                except ActionError:
                    raise
                except BaseException as error:
                    if (path / _COMPLETION_JOURNAL).is_file():
                        raise
                    _verify_generation_paths(pinned)
                    _fail_and_block(
                        path, manifest, job, "prototype-generation-failed", pinned=pinned
                    )
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    raise PrototypeJobError("prototype generation failed") from None
                finally:
                    pinned.close()
    except (ActionError, PrototypeJobError):
        raise
    except Exception:
        raise PrototypeJobError("prototype job validation failed") from None


def _prototype_prompt(contract: dict[str, Any], prototype: dict[str, Any]) -> bytes:
    coverage = ", ".join(prototype["covers_requirements"])
    view = next(
        (item.split(":", 1)[1] for item in prototype["covers_requirements"] if item.startswith("unsupported-view-anchor:")),
        "declared reference view",
    )
    bodies = ", ".join(item["kind"] for item in contract["character_construction"]["bodies"])
    roles = ", ".join(prototype["reference_roles"])
    return (
        f"# Prototype: {prototype['pose_id']}\n\n"
        f"Create one full-body 1:1 prototype for {contract['pet_id']} on a clean plain background.\n"
        f"Purpose: {prototype['purpose']}.\n"
        f"Evidence: {coverage}. View: {view}.\n"
        f"Reference roles: {roles}.\n"
        f"Preserve the design contract identity, proportions, palette, parts, and {bodies} construction.\n"
    ).encode("utf-8")


def _validated_state(run_dir: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    path = _validated_run_dir(run_dir)
    expected = _snapshot_generation_identities(path)
    pinned = _pin_generation_paths(path, expected=expected)
    try:
        return _validated_state_pinned(path, pinned)
    finally:
        pinned.close()


def _validated_state_pinned(
    path: Path, pinned: _PinnedGeneration
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    _verify_generation_paths(pinned)
    workflow = _read_json_at(pinned.descriptors["run"], "workflow.json")
    if workflow not in (
        {"schema_version": 2, "state": "prototyping", "blocked": None},
        {"schema_version": 2, "state": "awaiting_design_pack_approval", "blocked": None},
    ):
        raise ValueError("prototype workflow is not ready")
    artifact_bytes = _design_artifact_bytes_pinned(pinned)
    metadata = json.loads(artifact_bytes["omnipet-run.json"])
    intake = json.loads(artifact_bytes["design/intake.json"])
    plan_bytes = artifact_bytes["design/prototype-plan.json"]
    plan = json.loads(plan_bytes)
    contract = json.loads(artifact_bytes["design/design-contract.json"])
    rationale = artifact_bytes["design/design-rationale.md"].decode("utf-8")
    storyboard = json.loads(artifact_bytes["design/state-storyboard.json"])
    look = json.loads(artifact_bytes["design/look-mechanics.json"])
    manifest = _read_json_at(pinned.descriptors["run"], "imagegen-jobs.json")
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "design_artifacts", "jobs"} or manifest["schema_version"] != 2:
        raise ValueError("prototype manifest is invalid")
    hashes = {relative: hashlib.sha256(content).hexdigest() for relative, content in artifact_bytes.items()}
    if manifest["design_artifacts"] != hashes:
        raise ValueError("design artifacts changed")
    validate_design_documents(
        contract, rationale, storyboard, plan, look,
        pet_id=metadata["pet_id"], design_revision=metadata["design_revision"], intake=intake,
    )
    jobs = manifest["jobs"]
    if not isinstance(jobs, list):
        raise ValueError("prototype jobs are invalid")
    expected = build_prototype_publication(contract, plan, metadata, plan_bytes, hashes)
    expected_manifest = json.loads(expected["imagegen-jobs.json"])
    if [job.get("id") for job in jobs if isinstance(job, dict)] != [item["pose_id"] for item in plan["prototypes"]]:
        raise ValueError("prototype declarations changed")
    if any(job.get("id") in _STANDARD_IDS for job in jobs if isinstance(job, dict)):
        raise ValueError("standard jobs are not allowed")
    for job, expected_job in zip(jobs, expected_manifest["jobs"], strict=True):
        if not isinstance(job, dict) or set(job) != _JOB_KEYS:
            raise ValueError("prototype job is invalid")
        for key in _JOB_KEYS - {"status", "metadata"}:
            if job[key] != expected_job[key]:
                raise ValueError("prototype job contract changed")
        if job["status"] not in {"pending", "running", "complete", "failed"}:
            raise ValueError("prototype status is invalid")
        metadata_value = job["metadata"]
        allowed_metadata = {
            "pending": set(expected_job["metadata"]),
            "running": {*expected_job["metadata"], "attempts", "started_at"},
            "failed": {*expected_job["metadata"], "attempts", "started_at"},
            "complete": {
                *expected_job["metadata"], "attempts", "started_at", "source_path",
                "source_sha256", "completed_at", "principal",
            },
        }[job["status"]]
        if (
            not isinstance(metadata_value, dict)
            or set(metadata_value) != allowed_metadata
            or any(
            metadata_value.get(key) != value for key, value in expected_job["metadata"].items()
            )
        ):
            raise ValueError("prototype metadata is invalid")
        prompt_name = Path(job["prompt_file"]).name
        if hashlib.sha256(_read_at(pinned.descriptors["prompts"], prompt_name)).hexdigest() != metadata_value["prompt_sha256"]:
            raise ValueError("prototype prompt changed")
        for item in job["input_images"]:
            if hashlib.sha256(_read_at(pinned.descriptors["references"], Path(item["path"]).name)).hexdigest() != item["sha256"]:
                raise ValueError("prototype input changed")
        if job["status"] in {"pending", "running"} and _entry_exists(
            pinned.descriptors["generated"], f"{job['id']}.png"
        ):
            raise ValueError("prototype destination is unsafe")
        if job["status"] == "complete":
            output_parent = "decoded" if job["output_path"] == "decoded/canonical.png" else "decoded-prototypes"
            if hashlib.sha256(_read_at(pinned.descriptors[output_parent], Path(job["output_path"]).name)).hexdigest() != metadata_value.get("source_sha256"):
                raise ValueError("completed prototype changed")
            if not job["depends_on"]:
                if hashlib.sha256(_read_at(pinned.descriptors["references"], "canonical-base.png")).hexdigest() != metadata_value.get("source_sha256"):
                    raise ValueError("canonical promotion changed")
    return path, manifest, jobs


def _ready_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    statuses = {job["id"]: job["status"] for job in jobs}
    return [
        job for job in jobs
        if job["status"] == "pending"
        and all(statuses.get(dependency) == "complete" for dependency in job["depends_on"])
    ]


def _begin_attempt_pinned(
    manifest: dict[str, Any], job: dict[str, Any], pinned: _PinnedGeneration
) -> None:
    job["status"] = "running"
    job["metadata"]["attempts"] = int(job["metadata"].get("attempts", 0)) + 1
    job["metadata"]["started_at"] = datetime.now(timezone.utc).isoformat()
    _write_json_at_atomic(pinned.descriptors["run"], "imagegen-jobs.json", manifest)


def _request(
    run_dir: Path, job: dict[str, Any], pinned: _PinnedGeneration
) -> ImageRequest:
    grounding = []
    for item in job["input_images"]:
        path = run_dir / item["path"]
        content = _read_at(pinned.descriptors["references"], Path(item["path"]).name)
        grounding.append(_grounding_bytes(path, item["role"], item["sha256"], content))
    if job["depends_on"]:
        canonical = run_dir / "references/canonical-base.png"
        content = _read_at(pinned.descriptors["references"], "canonical-base.png")
        grounding.append(_grounding_bytes(canonical, "canonical identity", hashlib.sha256(content).hexdigest(), content))
    canvas = job["metadata"]["canvas"]
    return ImageRequest(
        prompt=_read_at(pinned.descriptors["prompts"], Path(job["prompt_file"]).name).decode("utf-8"),
        destination=run_dir / "generated-sources/prototypes" / f"{job['id']}.png",
        run_root=run_dir,
        grounding_images=tuple(grounding),
        aspect_ratio=canvas["aspect_ratio"], image_size=canvas["image_size"],
        task=job["id"],
    )


def _grounding_bytes(
    path: Path, role: str, expected_hash: str, content: bytes
) -> GroundingImage:
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError("grounding input changed")
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower())
    if mime is None:
        raise ValueError("grounding format is invalid")
    return GroundingImage(path, role, content, mime, expected_hash)


def _principal(generated: Any) -> str:
    metadata = getattr(generated, "metadata", {})
    value = metadata.get("principal") if hasattr(metadata, "get") else None
    return value if isinstance(value, str) and value.strip() else "image-generator"


def _fail_and_block(
    run_dir: Path, manifest: dict[str, Any], job: dict[str, Any], code: str,
    *, pinned: _PinnedGeneration | None = None,
) -> None:
    owned_pinned = pinned is None
    if pinned is None:
        pinned = _pin_generation_paths(run_dir)
    job["status"] = "failed"
    workflow = {
        "schema_version": 2,
        "state": "blocked",
        "blocked": {
            "code": code,
            "prior_state": "prototyping",
            "job_id": job["id"],
            "evidence_path": None,
            "root_failure_key": code,
            "recoveries": [],
            "diagnostic": None,
        },
    }
    journal = {
        "schema_version": 1,
        "state": "prepared",
        "job_id": job["id"],
        "directory_identities": _identity_record(run_dir, pinned),
        "manifest": manifest,
        "workflow": workflow,
    }
    try:
        _write_json_at_atomic(pinned.descriptors["run"], _FAILURE_JOURNAL, journal)
        _failure_boundary("prepared")
        _write_json_at_atomic(pinned.descriptors["run"], "imagegen-jobs.json", manifest)
        journal["state"] = "manifest-installed"
        _write_json_at_atomic(pinned.descriptors["run"], _FAILURE_JOURNAL, journal)
        _failure_boundary("manifest-installed")
        _write_json_at_atomic(pinned.descriptors["run"], "workflow.json", workflow)
        journal["state"] = "workflow-installed"
        _write_json_at_atomic(pinned.descriptors["run"], _FAILURE_JOURNAL, journal)
        _failure_boundary("workflow-installed")
        _finalize_failure_publication(run_dir, journal, pinned=pinned)
    finally:
        if owned_pinned:
            pinned.close()


def recover_prototype_jobs(
    run_dir: Path, *, pinned: _PinnedGeneration | None = None
) -> None:
    path = _validated_run_dir(run_dir)
    if pinned is None:
        run_descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            has_journal = any(
                _entry_exists(run_descriptor, filename)
                for filename in (_COMPLETION_JOURNAL, _FAILURE_JOURNAL)
            )
            has_v2_manifest = False
            if _entry_exists(run_descriptor, "imagegen-jobs.json"):
                manifest = _read_json_at(run_descriptor, "imagegen-jobs.json")
                has_v2_manifest = (
                    isinstance(manifest, dict) and manifest.get("schema_version") == 2
                )
            if not has_journal and not has_v2_manifest:
                return
        finally:
            os.close(run_descriptor)
        expected = _snapshot_generation_identities(path)
        with _generation_lock(path) as acquired:
            if not acquired:
                return
            _pre_pin_boundary()
            owned = _pin_generation_paths(path, expected=expected)
            try:
                recover_prototype_jobs(path, pinned=owned)
            finally:
                owned.close()
        return
    _verify_generation_paths(pinned)
    _recover_completion_publication(path, pinned=pinned)
    _recover_failure_publication(path, pinned=pinned)
    workflow = _read_json_at(pinned.descriptors["run"], "workflow.json")
    if workflow.get("schema_version") != 2 or workflow.get("state") != "prototyping":
        return
    manifest = _read_json_at(pinned.descriptors["run"], "imagegen-jobs.json")
    running = [job for job in manifest.get("jobs", []) if isinstance(job, dict) and job.get("status") == "running"]
    if not running:
        return
    if len(running) != 1 or not isinstance(running[0].get("metadata", {}).get("started_at"), str):
        raise ValueError("running prototype state is invalid")
    _fail_and_block(
        path, manifest, running[0], "prototype-attempt-interrupted", pinned=pinned
    )


def _recover_failure_publication(
    run_dir: Path, *, pinned: _PinnedGeneration
) -> None:
    if not _entry_exists(pinned.descriptors["run"], _FAILURE_JOURNAL):
        return
    current_manifest = _read_json_at(pinned.descriptors["run"], "imagegen-jobs.json")
    _validate_declared_manifest_pinned(run_dir, current_manifest, pinned)
    journal = _read_json_at(pinned.descriptors["run"], _FAILURE_JOURNAL)
    _validate_failure_recovery(journal, current_manifest)
    _verify_journal_identities(journal["directory_identities"], run_dir, pinned)
    _verify_generation_paths(pinned)
    _write_json_at_atomic(pinned.descriptors["run"], "imagegen-jobs.json", journal["manifest"])
    _write_json_at_atomic(pinned.descriptors["run"], "workflow.json", journal["workflow"])
    _verify_generation_paths(pinned)
    _finalize_failure_publication(run_dir, journal, pinned=pinned)


def _finalize_failure_publication(
    run_dir: Path, journal: dict[str, Any], *, pinned: _PinnedGeneration
) -> None:
    if _read_json_at(pinned.descriptors["run"], "imagegen-jobs.json") != journal["manifest"] or _read_json_at(pinned.descriptors["run"], "workflow.json") != journal["workflow"]:
        raise ValueError("prototype failure publication is incomplete")
    _unlink_at(pinned.descriptors["run"], _FAILURE_JOURNAL)


def _validate_failure_recovery(journal: Any, current_manifest: Any) -> None:
    if (
        not isinstance(journal, dict)
        or set(journal) != {"schema_version", "state", "job_id", "directory_identities", "manifest", "workflow"}
        or journal.get("schema_version") != 1
        or journal.get("state") not in {"prepared", "manifest-installed", "workflow-installed"}
        or not isinstance(journal.get("job_id"), str)
    ):
        raise ValueError("prototype failure journal is invalid")
    manifest = journal["manifest"]
    workflow = journal["workflow"]
    if (
        not isinstance(manifest, dict)
        or set(manifest) != set(current_manifest)
        or manifest.get("schema_version") != current_manifest.get("schema_version")
        or manifest.get("design_artifacts") != current_manifest.get("design_artifacts")
        or not isinstance(manifest.get("jobs"), list)
        or len(manifest["jobs"]) != len(current_manifest["jobs"])
        or not isinstance(workflow, dict)
        or set(workflow) != {"schema_version", "state", "blocked"}
        or workflow.get("schema_version") != 2
        or workflow.get("state") != "blocked"
        or not isinstance(workflow.get("blocked"), dict)
    ):
        raise ValueError("prototype failure payload is invalid")
    blocked = workflow["blocked"]
    if (
        set(blocked) != {
            "code", "prior_state", "job_id", "evidence_path", "root_failure_key",
            "recoveries", "diagnostic",
        }
        or blocked.get("prior_state") != "prototyping"
        or blocked.get("evidence_path") is not None
        or blocked.get("recoveries") != []
        or blocked.get("diagnostic") is not None
        or blocked.get("root_failure_key") != blocked.get("code")
        or blocked.get("job_id") != journal["job_id"]
        or blocked.get("code") not in {
            "prototype-generation-failed", "prototype-attempt-interrupted",
        }
    ):
        raise ValueError("prototype failure payload is invalid")
    matching = [
        index for index, job in enumerate(current_manifest["jobs"])
        if isinstance(job, dict) and job.get("id") == blocked.get("job_id")
    ]
    if len(matching) != 1:
        raise ValueError("prototype failure payload is invalid")
    active_index = matching[0]
    expected_running_keys = {"prompt_sha256", "canvas", "attempts", "started_at"}
    for index, (current, failed) in enumerate(
        zip(current_manifest["jobs"], manifest["jobs"], strict=True)
    ):
        if not isinstance(current, dict) or not isinstance(failed, dict):
            raise ValueError("prototype failure payload is invalid")
        if index != active_index:
            if failed != current:
                raise ValueError("prototype failure payload is invalid")
            continue
        if (
            failed.get("id") != blocked["job_id"]
            or failed.get("status") != "failed"
            or not isinstance(failed.get("metadata"), dict)
            or set(failed["metadata"]) != expected_running_keys
            or not isinstance(failed["metadata"].get("attempts"), int)
            or failed["metadata"]["attempts"] < 1
            or not isinstance(failed["metadata"].get("started_at"), str)
            or not failed["metadata"]["started_at"].strip()
        ):
            raise ValueError("prototype failure payload is invalid")
        expected_running = copy.deepcopy(failed)
        expected_running["status"] = "running"
        if current not in (expected_running, failed):
            raise ValueError("prototype failure payload is invalid")


def _failure_boundary(_state: str) -> None:
    pass


def _publish_completion(
    run_dir: Path, manifest: dict[str, Any], job: dict[str, Any], content: bytes,
    *, pinned: _PinnedGeneration | None = None,
) -> None:
    if pinned is None:
        raise ValueError("prototype completion requires pinned directories")
    digest = hashlib.sha256(content).hexdigest()
    journal = {
        "schema_version": 1,
        "state": "prepared",
        "job_id": job["id"],
        "sha256": digest,
        "content": base64.b64encode(content).decode("ascii"),
        "source_path": f"generated-sources/prototypes/{job['id']}.png",
        "output_path": job["output_path"],
        "canonical_path": None if job["depends_on"] else "references/canonical-base.png",
        "directory_identities": _identity_record(run_dir, pinned),
        "manifest": manifest,
    }
    _write_json_at_atomic(pinned.descriptors["run"], _COMPLETION_JOURNAL, journal)
    _completion_boundary("prepared")
    _install_completion(run_dir, journal, pinned=pinned)


def _install_completion(
    run_dir: Path, journal: dict[str, Any], *, pinned: _PinnedGeneration
) -> None:
    content = _completion_content(journal)
    if hashlib.sha256(_read_at(pinned.descriptors["generated"], Path(journal["source_path"]).name)).hexdigest() != journal["sha256"]:
        raise ValueError("prototype generated source changed")
    output_parent = "decoded" if journal["output_path"] == "decoded/canonical.png" else "decoded-prototypes"
    _write_at_atomic(pinned.descriptors[output_parent], Path(journal["output_path"]).name, content)
    journal["state"] = "output-installed"
    _write_json_at_atomic(pinned.descriptors["run"], _COMPLETION_JOURNAL, journal)
    _completion_boundary("output-installed")
    if journal["canonical_path"] is not None:
        _write_at_atomic(pinned.descriptors["references"], "canonical-base.png", content)
    journal["state"] = "canonical-installed"
    _write_json_at_atomic(pinned.descriptors["run"], _COMPLETION_JOURNAL, journal)
    _completion_boundary("canonical-installed")
    _write_json_at_atomic(pinned.descriptors["run"], "imagegen-jobs.json", journal["manifest"])
    journal["state"] = "manifest-installed"
    _write_json_at_atomic(pinned.descriptors["run"], _COMPLETION_JOURNAL, journal)
    _completion_boundary("manifest-installed")
    _verify_completion(journal, pinned)
    _unlink_at(pinned.descriptors["run"], _COMPLETION_JOURNAL)


def _recover_completion_publication(
    run_dir: Path, *, pinned: _PinnedGeneration
) -> None:
    if not _entry_exists(pinned.descriptors["run"], _COMPLETION_JOURNAL):
        return
    current_manifest = _read_json_at(pinned.descriptors["run"], "imagegen-jobs.json")
    _validate_declared_manifest_pinned(run_dir, current_manifest, pinned)
    journal = _read_json_at(pinned.descriptors["run"], _COMPLETION_JOURNAL)
    _validate_completion_recovery(journal, current_manifest)
    _verify_journal_identities(journal["directory_identities"], run_dir, pinned)
    _verify_generation_paths(pinned)
    _install_completion(run_dir, journal, pinned=pinned)
    _verify_generation_paths(pinned)


def _validate_declared_manifest(run_dir: Path, manifest: Any) -> None:
    expected = _snapshot_generation_identities(run_dir)
    pinned = _pin_generation_paths(run_dir, expected=expected)
    try:
        _validate_declared_manifest_pinned(run_dir, manifest, pinned)
    finally:
        pinned.close()


def _validate_declared_manifest_pinned(
    run_dir: Path, manifest: Any, pinned: _PinnedGeneration
) -> None:
    artifact_bytes = _design_artifact_bytes_pinned(pinned)
    hashes = {relative: hashlib.sha256(content).hexdigest() for relative, content in artifact_bytes.items()}
    metadata = json.loads(artifact_bytes["omnipet-run.json"])
    intake = json.loads(artifact_bytes["design/intake.json"])
    contract = json.loads(artifact_bytes["design/design-contract.json"])
    rationale = artifact_bytes["design/design-rationale.md"].decode("utf-8")
    storyboard = json.loads(artifact_bytes["design/state-storyboard.json"])
    plan_bytes = artifact_bytes["design/prototype-plan.json"]
    plan = json.loads(plan_bytes)
    look = json.loads(artifact_bytes["design/look-mechanics.json"])
    validate_design_documents(
        contract, rationale, storyboard, plan, look,
        pet_id=metadata["pet_id"], design_revision=metadata["design_revision"], intake=intake,
    )
    expected = json.loads(
        build_prototype_publication(contract, plan, metadata, plan_bytes, hashes)["imagegen-jobs.json"]
    )
    if (
        not isinstance(manifest, dict)
        or set(manifest) != set(expected)
        or manifest.get("schema_version") != expected["schema_version"]
        or manifest.get("design_artifacts") != expected["design_artifacts"]
        or not isinstance(manifest.get("jobs"), list)
        or len(manifest["jobs"]) != len(expected["jobs"])
    ):
        raise ValueError("prototype manifest declarations changed")
    for current, declared in zip(manifest["jobs"], expected["jobs"], strict=True):
        if not isinstance(current, dict) or set(current) != _JOB_KEYS:
            raise ValueError("prototype manifest declarations changed")
        if any(current[key] != declared[key] for key in _JOB_KEYS - {"status", "metadata"}):
            raise ValueError("prototype manifest declarations changed")
        metadata_value = current.get("metadata")
        allowed_metadata = {
            "pending": set(declared["metadata"]),
            "running": {*declared["metadata"], "attempts", "started_at"},
            "failed": {*declared["metadata"], "attempts", "started_at"},
            "complete": {
                *declared["metadata"], "attempts", "started_at", "source_path",
                "source_sha256", "completed_at", "principal",
            },
        }.get(current.get("status"))
        if (
            not isinstance(metadata_value, dict)
            or allowed_metadata is None
            or set(metadata_value) != allowed_metadata
            or any(metadata_value.get(key) != value for key, value in declared["metadata"].items())
        ):
            raise ValueError("prototype manifest declarations changed")


def _validate_completion_recovery(journal: Any, current_manifest: Any) -> None:
    content = _completion_content(journal)
    if (
        not isinstance(current_manifest, dict)
        or set(current_manifest) != {"schema_version", "design_artifacts", "jobs"}
        or current_manifest.get("schema_version") != 2
        or not isinstance(current_manifest.get("jobs"), list)
        or not isinstance(journal["job_id"], str)
    ):
        raise ValueError("prototype completion recovery is invalid")
    matching = [
        (index, job) for index, job in enumerate(current_manifest["jobs"])
        if isinstance(job, dict) and job.get("id") == journal["job_id"]
    ]
    if len(matching) != 1:
        raise ValueError("prototype completion recovery is invalid")
    index, current_job = matching[0]
    expected_source = f"generated-sources/prototypes/{current_job['id']}.png"
    expected_output = current_job.get("output_path")
    expected_canonical = (
        "references/canonical-base.png" if current_job.get("depends_on") == [] else None
    )
    for value, expected in (
        (journal["source_path"], expected_source),
        (journal["output_path"], expected_output),
        (journal["canonical_path"], expected_canonical),
    ):
        if value != expected or (value is not None and not _is_safe_relative(value)):
            raise ValueError("prototype completion recovery is invalid")
    embedded = journal["manifest"]
    if (
        not isinstance(embedded, dict)
        or set(embedded) != set(current_manifest)
        or embedded.get("schema_version") != current_manifest["schema_version"]
        or embedded.get("design_artifacts") != current_manifest["design_artifacts"]
        or not isinstance(embedded.get("jobs"), list)
        or len(embedded["jobs"]) != len(current_manifest["jobs"])
    ):
        raise ValueError("prototype completion recovery is invalid")
    completed_job = embedded["jobs"][index]
    if not isinstance(completed_job, dict) or completed_job.get("id") != journal["job_id"]:
        raise ValueError("prototype completion recovery is invalid")
    completion_fields = {"source_path", "source_sha256", "completed_at", "principal"}
    completed_metadata = completed_job.get("metadata")
    expected_running_keys = {"prompt_sha256", "canvas", "attempts", "started_at"}
    expected_metadata_keys = expected_running_keys | completion_fields
    if (
        completed_job.get("status") != "complete"
        or not isinstance(completed_metadata, dict)
        or set(completed_metadata) != expected_metadata_keys
        or completed_metadata.get("source_path") != expected_source
        or completed_metadata.get("source_sha256") != journal["sha256"]
        or not isinstance(completed_metadata.get("completed_at"), str)
        or not completed_metadata["completed_at"].strip()
        or not isinstance(completed_metadata.get("principal"), str)
        or not completed_metadata["principal"].strip()
    ):
        raise ValueError("prototype completion recovery is invalid")
    prior_job = copy.deepcopy(completed_job)
    prior_job["status"] = "running"
    for key in completion_fields:
        prior_job["metadata"].pop(key)
    if set(prior_job["metadata"]) != expected_running_keys:
        raise ValueError("prototype completion recovery is invalid")
    expected_manifest = copy.deepcopy(current_manifest)
    expected_manifest["jobs"][index] = completed_job
    if embedded != expected_manifest:
        raise ValueError("prototype completion recovery is invalid")
    if current_job != prior_job and current_job != completed_job:
        raise ValueError("prototype completion recovery is invalid")
    if hashlib.sha256(content).hexdigest() != journal["sha256"]:
        raise ValueError("prototype completion recovery is invalid")


def _is_safe_relative(value: str) -> bool:
    path = Path(value)
    return (
        bool(value) and "\\" not in value and not path.is_absolute()
        and path != Path(".") and ".." not in path.parts
        and path.as_posix() == value and all(part not in {"", "."} for part in path.parts)
    )


def _completion_content(journal: Any) -> bytes:
    if (
        not isinstance(journal, dict)
        or set(journal) != {
            "schema_version", "state", "job_id", "sha256", "content", "source_path", "output_path",
            "canonical_path", "directory_identities", "manifest",
        }
        or journal["schema_version"] != 1
        or journal["state"] not in {"prepared", "output-installed", "canonical-installed", "manifest-installed"}
    ):
        raise ValueError("prototype completion journal is invalid")
    content = base64.b64decode(journal["content"], validate=True)
    _validate_png_bytes(content)
    if hashlib.sha256(content).hexdigest() != journal["sha256"]:
        raise ValueError("prototype completion content changed")
    return content


def _verify_completion(journal: dict[str, Any], pinned: _PinnedGeneration) -> None:
    digest = journal["sha256"]
    output_parent = "decoded" if journal["output_path"] == "decoded/canonical.png" else "decoded-prototypes"
    if hashlib.sha256(_read_at(pinned.descriptors[output_parent], Path(journal["output_path"]).name)).hexdigest() != digest:
        raise ValueError("prototype completion target changed")
    if journal["canonical_path"] is not None and hashlib.sha256(_read_at(pinned.descriptors["references"], "canonical-base.png")).hexdigest() != digest:
        raise ValueError("prototype completion target changed")
    if _read_json_at(pinned.descriptors["run"], "imagegen-jobs.json") != journal["manifest"]:
        raise ValueError("prototype completion manifest changed")


def _completion_boundary(_state: str) -> None:
    pass


def _generation_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "run": run_dir, "generated": run_dir / "generated-sources/prototypes",
        "decoded": run_dir / "decoded", "decoded-prototypes": run_dir / "decoded/prototypes",
        "references": run_dir / "references", "prompts": run_dir / "prompts/prototypes",
        "design": run_dir / "design",
    }


def _snapshot_generation_identities(
    run_dir: Path,
) -> dict[str, tuple[int, int]]:
    result = {}
    for name, path in _generation_paths(run_dir).items():
        value = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(value.st_mode):
            raise ValueError("prototype directory is unsafe")
        result[name] = (value.st_dev, value.st_ino)
    return result


def _pin_generation_paths(
    run_dir: Path, *, expected: dict[str, tuple[int, int]] | None = None
) -> _PinnedGeneration:
    paths = _generation_paths(run_dir)
    identities = []
    descriptors = {}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for name, path in paths.items():
            descriptor = os.open(path, flags)
            descriptors[name] = descriptor
            value = os.fstat(descriptor)
            if expected is not None and expected.get(name) != (value.st_dev, value.st_ino):
                raise ValueError("prototype directory changed before pin")
            identities.append((path, value.st_dev, value.st_ino))
        return _PinnedGeneration(tuple(identities), descriptors)
    except Exception:
        for descriptor in descriptors.values():
            os.close(descriptor)
        raise


def _verify_generation_paths(pinned: _PinnedGeneration) -> None:
    for path, device, inode in pinned.identities:
        value = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != (device, inode):
            raise ValueError("prototype directory changed")


def _verify_journal_identities(
    value: Any, run_dir: Path, pinned: _PinnedGeneration
) -> None:
    expected_names = set(_generation_paths(run_dir))
    if not isinstance(value, dict) or set(value) != expected_names:
        raise ValueError("prototype journal directory identities are invalid")
    actual = _identity_record(run_dir, pinned)
    if value != actual:
        raise ValueError("prototype journal directory identities changed")


def _read_at(directory_fd: int, filename: str) -> bytes:
    descriptor = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("prototype artifact is unsafe")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_json_at(directory_fd: int, filename: str) -> Any:
    return json.loads(_read_at(directory_fd, filename).decode("utf-8"))


def _write_json_at_atomic(directory_fd: int, filename: str, payload: Any) -> None:
    _write_at_atomic(directory_fd, filename, _json_bytes(payload))


def _unlink_at(directory_fd: int, filename: str) -> None:
    os.unlink(filename, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _entry_exists(directory_fd: int, filename: str) -> bool:
    try:
        value = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("prototype artifact is unsafe")
    return True


def _design_artifact_bytes_pinned(pinned: _PinnedGeneration) -> dict[str, bytes]:
    return {
        "omnipet-run.json": _read_at(pinned.descriptors["run"], "omnipet-run.json"),
        "design/intake.json": _read_at(pinned.descriptors["design"], "intake.json"),
        "design/design-contract.json": _read_at(pinned.descriptors["design"], "design-contract.json"),
        "design/design-rationale.md": _read_at(pinned.descriptors["design"], "design-rationale.md"),
        "design/state-storyboard.json": _read_at(pinned.descriptors["design"], "state-storyboard.json"),
        "design/prototype-plan.json": _read_at(pinned.descriptors["design"], "prototype-plan.json"),
        "design/look-mechanics.json": _read_at(pinned.descriptors["design"], "look-mechanics.json"),
    }


def _pre_pin_boundary() -> None:
    pass


def _write_at_atomic(directory_fd: int, filename: str, content: bytes) -> None:
    temporary = f".{filename}.tmp-{os.getpid()}-{id(content)}"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600, dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


@contextmanager
def _generation_lock(run_dir: Path):
    lock_path = run_dir / ".prototype-generation.lock"
    existed = lock_path.exists()
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        if not existed:
            _fsync_directory(run_dir)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            _validate_lock_identity(descriptor, path=lock_path)
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            try:
                _validate_lock_identity(descriptor, path=lock_path)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validated_run_dir(run_dir: Path) -> Path:
    path = Path(run_dir).absolute()
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ValueError("run directory is unsafe")
    return path


def _safe_file(run_dir: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("prototype path is invalid")
    path = run_dir / relative
    current = run_dir
    for part in Path(relative).parts[:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise ValueError("prototype path is unsafe")
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(run_dir):
        raise ValueError("prototype file is unsafe")
    return path


def _read_json(path: Path) -> Any:
    return json.loads(_read_regular(path).decode("utf-8"))


def _read_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("prototype artifact is unsafe")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_png_bytes(content: bytes) -> None:
    import io
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG":
                raise ValueError("prototype is not PNG")
            image.verify()
    except (OSError, SyntaxError, UnidentifiedImageError):
        raise ValueError("prototype is not PNG") from None


def _write_json_atomic(path: Path, payload: Any) -> None:
    _write_bytes_atomic(path, _json_bytes(payload))


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("prototype destination is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
