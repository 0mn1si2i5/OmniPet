from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from omnipet.actions import ActionError, validate_action_request_unlocked
from omnipet.design_contracts import validate_design_documents
from omnipet.prototype_jobs import (
    _validate_png_bytes,
    build_prototype_publication,
    validated_prototype_manifest,
)
from omnipet.security import contains_credential_like_text
from omnipet.workflow import (
    PHASE2_TRANSITIONS,
    WorkflowState,
    _fsync_directory,
    _validate_lock_identity,
    _validate_workflow_v2,
    _workflow_lock,
)


_INTAKE_KEYS = {
    "schema_version",
    "pet_id",
    "design_revision",
    "references",
    "rights",
    "budget",
    "style_request",
    "observed_facts",
    "inferred_facts",
    "unknowns",
    "accepted_defaults",
    "owner_decisions",
}
_METADATA_KEYS = {"schema_version", "pet_id", "design_revision", "references"}
_REFERENCE_KEYS = {"path", "role", "sha256"}
_METADATA_REFERENCE_KEYS = {"run_path", "role", "sha256"}
_RIGHTS_KEYS = {"status", "note"}
_BUDGET_KEYS = {"authorized_usd", "estimated_provider_calls"}
_FACT_FIELDS = (
    "observed_facts",
    "inferred_facts",
    "unknowns",
    "accepted_defaults",
    "owner_decisions",
)
_SHA256_LENGTH = 64
_PET_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_DESIGN_REVISION = re.compile(r"design-[0-9]{4}")
_JOURNAL_NAME = "design-intake-publication.json"
_INTAKE_BACKUP = ".design-intake.previous"
_INTAKE_ABSENT = ".design-intake.previous.absent"
_WORKFLOW_BACKUP = ".design-workflow.previous"
_PREPARE_MARKER = ".design-intake.prepare"
_JOURNAL_KEYS = {
    "schema_version",
    "state",
    "intake_path",
    "workflow_path",
    "intake_backup_path",
    "intake_absent_path",
    "workflow_backup_path",
    "intake_previously_existed",
    "previous_intake_sha256",
    "previous_workflow_sha256",
    "installed_intake_sha256",
    "installed_workflow_sha256",
}
_JOURNAL_STATES = {
    "prepared",
    "intake-installed",
    "workflow-installed",
    "rollback-restored",
}
_DESIGN_FILES = {
    "design-contract.json": "contract",
    "design-rationale.md": "rationale",
    "state-storyboard.json": "storyboard",
    "prototype-plan.json": "prototype_plan",
    "look-mechanics.json": "look_mechanics",
}
_DESIGN_JOURNAL = "design-publication.json"
_DESIGN_PREPARE = ".design-publication.prepare"
_DESIGN_JOURNAL_KEYS = {"schema_version", "state", "files", "directories", "workflow"}
_DESIGN_DIRECTORIES = ("prompts", "prompts/prototypes", "generated-sources", "generated-sources/prototypes")
_VERDICT_CATEGORIES = ("structural", "view-semantic", "identity", "pose-purpose")
_PROTOTYPE_EVIDENCE_KEYS = {
    "schema_version", "pet_id", "design_revision", "pose_id", "artifact",
    "verdicts", "accepted_warnings",
}
_VERDICT_KEYS = {"decision", "reviewer_role", "reviewer_principal_id", "criteria"}
_REVIEWER_ROLES = {
    "deterministic", "production-agent", "independent-visual-reviewer", "owner",
}
_SUMMARY_KEYS = {
    "schema_version", "decision", "known_risks", "accepted_warnings",
    "expected_provider_calls", "budget_authorized_usd", "reviewer_principal_id",
    "reviewed_at", "evidence",
}
_DESIGN_PACK_PATHS = (
    "omnipet-run.json", "design/intake.json", "design/design-contract.json",
    "design/design-rationale.md", "design/state-storyboard.json",
    "design/prototype-plan.json", "design/look-mechanics.json",
)
_STANDARD_ROW_IDS = (
    "idle", "running-right", "running-left", "waving", "jumping",
    "failed", "waiting", "running", "review",
)
_STANDARD_MANIFEST_KEYS = {
    "schema_version", "manifest_kind", "design_revision", "design_pack_sha256", "jobs",
}
_STANDARD_JOB_BASE_KEYS = {
    "id", "kind", "status", "depends_on", "design_revision", "design_pack_sha256",
    "prompt_file", "retry_prompt_file", "input_images", "output_path", "canvas", "metadata",
}


@dataclass
class _PinnedDirs:
    run_dir: Path
    design_dir: Path
    run_fd: int
    design_fd: int
    references_fd: int
    run_identity: tuple[int, int]
    design_identity: tuple[int, int]
    references_identity: tuple[int, int]

    def close(self) -> None:
        os.close(self.references_fd)
        os.close(self.design_fd)
        os.close(self.run_fd)


class DesignPackError(RuntimeError):
    """Raised when a Design Pack operation cannot be completed safely."""


def submit_intake_action(
    run_dir: Path, payload: Any, *, action_id: str, run_revision: str,
) -> WorkflowState:
    return submit_intake(
        run_dir, payload, action_id=action_id, run_revision=run_revision
    )


def submit_design_action(
    run_dir: Path, *, action_id: str, run_revision: str, contract: Any,
    rationale: Any, storyboard: Any, prototype_plan: Any, look_mechanics: Any,
) -> WorkflowState:
    return submit_design(
        run_dir, contract=contract, rationale=rationale, storyboard=storyboard,
        prototype_plan=prototype_plan, look_mechanics=look_mechanics,
        action_id=action_id, run_revision=run_revision,
    )


def submit_prototype_evidence_action(
    run_dir: Path, payload: Any, *, action_id: str, run_revision: str,
) -> WorkflowState:
    return submit_prototype_evidence(
        run_dir, payload, action_id=action_id, run_revision=run_revision
    )


def submit_design_pack_summary_action(
    run_dir: Path, contact_sheet_path: Path, review_payload: Any, *,
    action_id: str, run_revision: str,
) -> WorkflowState:
    return submit_design_pack_summary(
        run_dir, contact_sheet_path, review_payload,
        action_id=action_id, run_revision=run_revision,
    )


def approve_design_pack_action(
    run_dir_or_project: Any, owner_principal_id: str, note: str | None = None, *,
    action_id: str, run_revision: str,
) -> WorkflowState:
    run_dir = _design_run_dir(run_dir_or_project)
    return approve_design_pack(
        run_dir, owner_principal_id, note,
        action_id=action_id, run_revision=run_revision,
    )


def revise_design_pack_action(
    run_dir: Path, reason: str, *, action_id: str, run_revision: str,
) -> WorkflowState:
    return _decide_design_pack(
        run_dir, "revise-design-pack", reason, action_id, run_revision
    )


def reject_design_pack_action(
    run_dir: Path, reason: str, *, action_id: str, run_revision: str,
) -> WorkflowState:
    return _decide_design_pack(
        run_dir, "reject-design-pack", reason, action_id, run_revision
    )


def _validate_action_if_required(
    run_dir: Path, action_id: str | None, run_revision: str | None, kind: str,
) -> dict[str, Any] | None:
    if action_id is None and run_revision is None:
        return None
    if action_id is None or run_revision is None:
        raise ActionError("action request is invalid")
    return validate_action_request_unlocked(run_dir, action_id, run_revision, kind)


def _decide_design_pack(
    run_dir: Path, kind: str, reason: str, action_id: str, run_revision: str,
) -> WorkflowState:
    path = _validated_run_dir(run_dir)
    if not isinstance(reason, str) or not reason.strip():
        raise ActionError("action request is invalid")
    with _workflow_lock(path):
        _validate_action_if_required(path, action_id, run_revision, kind)
        workflow = _read_json(path / "workflow.json")
        if workflow != {
            "schema_version": 2,
            "state": "awaiting_design_pack_approval",
            "blocked": None,
        }:
            raise ActionError("action request does not match")
        if kind == "reject-design-pack":
            result = WorkflowState("rejected")
            _write_bytes_atomic(path / "workflow.json", _canonical_json_bytes({
                "schema_version": 2, "state": result.state, "blocked": None,
            }))
            return result

        removable = [
            path / "imagegen-jobs.json",
            path / "qa/approvals-v2.json",
            path / "qa/design-pack/contact-sheet.png",
            path / "qa/design-pack/review.json",
            path / "design/design-pack.json",
            path / "decoded/canonical.png",
            *(path / "qa/design-pack/prototypes").glob("*.json"),
            *(path / "prompts/prototypes").glob("*"),
            *(path / "prompts/rows").glob("*"),
            *(path / "prompts/row-retries").glob("*"),
            *(path / "decoded/prototypes").glob("*"),
        ]
        before = {target: _optional_regular_bytes(target) for target in removable}
        workflow_before = _read_regular_bytes(path / "workflow.json")
        try:
            for target in removable:
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise ValueError("design revision path is unsafe")
                target.unlink(missing_ok=True)
            result = WorkflowState("designing")
            _write_bytes_atomic(path / "workflow.json", _canonical_json_bytes({
                "schema_version": 2, "state": result.state, "blocked": None,
            }))
            return result
        except Exception:
            for target, content in before.items():
                if content is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _write_bytes_atomic(target, content)
            _write_bytes_atomic(path / "workflow.json", workflow_before)
            raise


def approve_design_pack(
    run_dir_or_project: Any,
    owner_principal_id: str,
    note: str | None = None,
    *,
    action_id: str | None = None,
    run_revision: str | None = None,
) -> WorkflowState:
    from omnipet.approvals import (
        ArtifactHash, DesignPackApproval, _write_design_pack_approval,
    )
    from omnipet.run import STANDARD_JOB_IDS, build_current_prompts

    try:
        run_dir = _design_run_dir(run_dir_or_project)
        _validate_text(owner_principal_id)
        if note is not None:
            _validate_text(note)
        with _workflow_lock(run_dir):
            _validate_action_if_required(
                run_dir, action_id, run_revision, "approve-design-pack"
            )
            if _read_json(run_dir / "workflow.json") != {
                "schema_version": 2,
                "state": "awaiting_design_pack_approval",
                "blocked": None,
            }:
                raise ValueError("workflow is not awaiting design pack approval")
            pack, prototype_manifest, contract, storyboard, plan, intake = _validated_design_pack(run_dir)
            artifact_hashes = {
                item["path"]: item["sha256"] for item in pack["artifacts"]
            }
            pack_hash = _hash_regular_file(run_dir / "design/design-pack.json")
            approval = DesignPackApproval(
                stage="design-pack",
                artifacts=tuple(
                    ArtifactHash(path, digest)
                    for path, digest in sorted({
                        **artifact_hashes,
                        "design/design-pack.json": pack_hash,
                    }.items())
                ),
                approved_at=datetime.now(timezone.utc).isoformat(),
                owner_principal_id=owner_principal_id.strip(),
                note=note.strip() if note is not None else None,
            )
            anchors_by_state = _standard_row_anchor_paths(contract, plan)
            request = {
                "pet_notes": "; ".join(intake["observed_facts"]),
                "style_contract": intake["style_request"],
                "chroma_key": {"hex": "#00FFFF", "name": "cyan"},
                "rows": [],
            }
            project = SimpleNamespace(
                pet_id=pack["pet_id"], display_name=pack["pet_id"].replace("-", " ").title()
            )
            prompt_bytes = {}
            reference_inputs = [
                {"path": item["path"], "role": item["role"], "sha256": item["sha256"]}
                for item in intake["references"]
            ]
            jobs = []
            for job_id in STANDARD_JOB_IDS:
                prompts = build_current_prompts(
                    project,
                    request,
                    design_context={
                        "contract": contract,
                        "storyboard": storyboard,
                        "pose_anchors": anchors_by_state[job_id],
                    },
                )
                prompt_file = f"prompts/rows/{job_id}.md"
                retry_file = f"prompts/row-retries/{job_id}.md"
                prompt_bytes[prompt_file] = prompts[f"rows/{job_id}.md"].encode("utf-8")
                prompt_bytes[retry_file] = prompts[f"row-retries/{job_id}.md"].encode("utf-8")
                inputs = [*reference_inputs, *(
                    {
                        "path": anchor,
                        "role": (
                            "approved canonical" if anchor == "decoded/canonical.png"
                            else "approved pose anchor"
                        ),
                        "sha256": _hash_regular_file(run_dir / anchor),
                    }
                    for anchor in anchors_by_state[job_id]
                )]
                jobs.append({
                    "id": job_id,
                    "kind": "row-strip",
                    "status": "pending",
                    "depends_on": [],
                    "design_revision": pack["design_revision"],
                    "design_pack_sha256": pack_hash,
                    "prompt_file": prompt_file,
                    "retry_prompt_file": retry_file,
                    "input_images": [dict(item) for item in inputs],
                    "output_path": f"decoded/{job_id}.png",
                    "canvas": {"aspect_ratio": "21:9", "image_size": "2K"},
                    "metadata": {
                        "prompt_sha256": _sha256_bytes(prompt_bytes[prompt_file]),
                        "retry_prompt_sha256": _sha256_bytes(prompt_bytes[retry_file]),
                    },
                })
            manifest = {
                "schema_version": 2, "manifest_kind": "standard-rows",
                "design_revision": pack["design_revision"],
                "design_pack_sha256": pack_hash,
                "jobs": jobs,
            }
            targets = {
                run_dir / "qa/approvals-v2.json",
                run_dir / "imagegen-jobs.json",
                run_dir / "workflow.json",
                *(run_dir / relative for relative in prompt_bytes),
            }
            before = {target: _optional_regular_bytes(target) for target in targets}
            created_directories: list[Path] = []
            try:
                for relative, content in prompt_bytes.items():
                    parent = (run_dir / relative).parent
                    if not parent.exists():
                        parent.mkdir(parents=True)
                        created_directories.append(parent)
                    _write_bytes_atomic(run_dir / relative, content)
                _write_design_pack_approval(run_dir, approval)
                _write_bytes_atomic(run_dir / "imagegen-jobs.json", _canonical_json_bytes(manifest))
                context = _pin_directories(run_dir)
                try:
                    result = _transition_workflow_pinned(context, "design-pack-approved")
                finally:
                    context.close()
                _validate_current_design_pack_approval(run_dir)
                return result
            except Exception:
                for target, content in before.items():
                    if content is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _write_bytes_atomic(target, content)
                for directory in sorted(set(created_directories), key=lambda item: len(item.parts), reverse=True):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                raise
    except ActionError:
        raise
    except Exception:
        raise DesignPackError("design pack approval failed") from None


def _design_run_dir(value: Any) -> Path:
    if isinstance(value, (str, Path)):
        return _validated_run_dir(Path(value))
    pet_id = getattr(value, "pet_id", None)
    repository_root = getattr(value, "repository_root", None)
    if not isinstance(pet_id, str) or repository_root is None:
        raise ValueError("design run is invalid")
    return _validated_run_dir(Path(repository_root) / ".omnipet/runs" / pet_id)


def _validated_design_pack(
    run_dir: Path,
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any],
    dict[str, Any], dict[str, Any],
]:
    pack = _read_json(run_dir / "design/design-pack.json")
    _validate_design_pack_manifest(pack)
    for item in pack["artifacts"]:
        if _hash_regular_file(run_dir / item["path"]) != item["sha256"]:
            raise ValueError("design pack artifact changed")
    prototype_manifest = validated_prototype_manifest(run_dir)
    if any(job["status"] != "complete" for job in prototype_manifest["jobs"]):
        raise ValueError("prototype jobs are incomplete")
    if _read_json(run_dir / "design/prototype-jobs-approved.json") != prototype_manifest:
        raise ValueError("prototype manifest snapshot changed")
    metadata = _read_json(run_dir / "omnipet-run.json")
    intake = _read_json(run_dir / "design/intake.json")
    contract = _read_json(run_dir / "design/design-contract.json")
    rationale = _read_regular_bytes(run_dir / "design/design-rationale.md").decode("utf-8")
    storyboard = _read_json(run_dir / "design/state-storyboard.json")
    plan = _read_json(run_dir / "design/prototype-plan.json")
    look = _read_json(run_dir / "design/look-mechanics.json")
    validate_design_documents(
        contract, rationale, storyboard, plan, look,
        pet_id=metadata["pet_id"], design_revision=metadata["design_revision"], intake=intake,
    )
    reviews, warnings = _validated_prototype_reviews(run_dir, prototype_manifest)
    review = _read_json(run_dir / "qa/design-pack/review.json")
    _validate_summary_shape(review)
    if review["accepted_warnings"] != warnings or review["evidence"]["prototype_reviews"] != [
        {"path": path, "sha256": digest} for path, digest in sorted(reviews.items())
    ]:
        raise ValueError("design pack review changed")
    if review["evidence"]["contact_sheet_sha256"] != _hash_regular_file(run_dir / "qa/design-pack/contact-sheet.png"):
        raise ValueError("design pack contact sheet changed")
    return pack, prototype_manifest, contract, storyboard, plan, intake


def _standard_row_anchor_paths(
    contract: dict[str, Any], plan: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    prototypes = {item["pose_id"]: item for item in plan["prototypes"]}
    paths = {
        pose_id: (
            "decoded/canonical.png"
            if item["evidence_kind"] == "animation-ready-canonical"
            else f"decoded/prototypes/{pose_id}.png"
        )
        for pose_id, item in prototypes.items()
    }
    selected = {state: set() for state in _STANDARD_ROW_IDS}
    for pose_id, prototype in prototypes.items():
        coverage = set(prototype["covers_requirements"])
        if coverage & {"animation-ready-canonical", "motion-cycle"}:
            for state in selected:
                selected[state].add(pose_id)
        if "screen-left-anchor" in coverage:
            selected["running-left"].add(pose_id)
        if "screen-right-anchor" in coverage:
            selected["running-right"].add(pose_id)
        if coverage & {"grounded-anticipation", "airborne", "return-pose"}:
            selected["jumping"].add(pose_id)
        for requirement in coverage:
            if requirement.startswith("state-extreme-anchor:"):
                state = requirement.split(":", 1)[1]
                if state in selected:
                    selected[state].add(pose_id)
            if requirement.startswith("unsupported-view-anchor:"):
                view = requirement.split(":", 1)[1]
                for item in contract["reference_view_coverage"]["unsupported"]:
                    if item["view"] == view:
                        for state in item["dependent_states"]:
                            selected[state].add(pose_id)
    for risk in contract["generation_risks"]:
        if risk["risk"] != "compound_character":
            continue
        attachment = [
            pose_id for pose_id, prototype in prototypes.items()
            if "stable-attachment" in prototype["covers_requirements"]
        ]
        for state in risk["affected_states"]:
            selected[state].update(attachment)
    for state, grammar in contract["state_grammar"].items():
        for value in grammar.values():
            if value in prototypes:
                selected[state].add(value)
    order = list(prototypes)
    return {
        state: tuple(paths[pose_id] for pose_id in order if pose_id in pose_ids)
        for state, pose_ids in selected.items()
    }


def _validate_current_design_pack_approval(run_dir: Path) -> None:
    from omnipet.approvals import load_design_pack_approvals

    try:
        path = _validated_run_dir(run_dir)
        records = load_design_pack_approvals(path)
        if len(records) != 1:
            raise ValueError("design approval is missing")
        record = records[0]
        artifacts = {item.path: item.sha256 for item in record.artifacts}
        pack_hash = _hash_regular_file(path / "design/design-pack.json")
        if artifacts.get("design/design-pack.json") != pack_hash:
            raise ValueError("design approval is stale")
        pack = _read_json(path / "design/design-pack.json")
        _validate_design_pack_manifest(pack)
        for item in pack["artifacts"]:
            if artifacts.get(item["path"]) != item["sha256"]:
                raise ValueError("design approval is incomplete")
            if _hash_regular_file(path / item["path"]) != item["sha256"]:
                raise ValueError("design approval is stale")
        contract = _read_json(path / "design/design-contract.json")
        plan = _read_json(path / "design/prototype-plan.json")
        intake = _read_json(path / "design/intake.json")
        expected_anchors = _standard_row_anchor_paths(contract, plan)
        expected_references = [
            {"path": item["path"], "role": item["role"], "sha256": item["sha256"]}
            for item in intake["references"]
        ]
        manifest = _read_json(path / "imagegen-jobs.json")
        if (
            set(manifest) != _STANDARD_MANIFEST_KEYS
            or manifest.get("schema_version") != 2
            or manifest.get("manifest_kind") != "standard-rows"
            or manifest.get("design_revision") != pack["design_revision"]
            or manifest.get("design_pack_sha256") != pack_hash
            or [job.get("id") for job in manifest.get("jobs", ())] != list(_STANDARD_ROW_IDS)
        ):
            raise ValueError("standard manifest is not approved")
        for job_id, job in zip(_STANDARD_ROW_IDS, manifest["jobs"], strict=True):
            status = job.get("status") if isinstance(job, dict) else None
            extra_keys = {
                "pending": set(), "running": set(), "failed": set(),
                "complete": {"source_path", "completed_at"},
            }.get(status)
            if extra_keys is None or set(job) != _STANDARD_JOB_BASE_KEYS | extra_keys:
                raise ValueError("standard job schema is invalid")
            if (
                job["id"] != job_id or job["kind"] != "row-strip"
                or job["depends_on"] != []
                or job["design_revision"] != pack["design_revision"]
                or job["design_pack_sha256"] != pack_hash
                or job["prompt_file"] != f"prompts/rows/{job_id}.md"
                or job["retry_prompt_file"] != f"prompts/row-retries/{job_id}.md"
                or job["output_path"] != f"decoded/{job_id}.png"
                or job["canvas"] != {"aspect_ratio": "21:9", "image_size": "2K"}
            ):
                raise ValueError("standard job contract is invalid")
            expected_inputs = [*expected_references, *(
                {
                    "path": anchor,
                    "role": "approved canonical" if anchor == "decoded/canonical.png" else "approved pose anchor",
                    "sha256": _hash_regular_file(path / anchor),
                }
                for anchor in expected_anchors[job_id]
            )]
            if job["input_images"] != expected_inputs:
                raise ValueError("standard job inputs are invalid")
            metadata = job["metadata"]
            required_metadata = {"prompt_sha256", "retry_prompt_sha256"}
            allowed_metadata = {
                "pending": required_metadata,
                "running": required_metadata | {"attempts", "started_at", "generation_guides"},
                "failed": required_metadata | {"attempts", "started_at", "generation_guides"},
                "complete": required_metadata | {"attempts", "started_at", "generation_guides"},
            }[status]
            if not isinstance(metadata, dict) or set(metadata) != allowed_metadata:
                raise ValueError("standard job metadata is invalid")
            if (
                metadata["prompt_sha256"] != _hash_regular_file(path / job["prompt_file"])
                or metadata["retry_prompt_sha256"] != _hash_regular_file(path / job["retry_prompt_file"])
            ):
                raise ValueError("standard job prompt is invalid")
    except Exception:
        raise DesignPackError("design pack approval is invalid") from None


def submit_prototype_evidence(
    run_dir: Path, payload: Any, *, action_id: str | None = None,
    run_revision: str | None = None,
) -> WorkflowState:
    try:
        path = _validated_run_dir(run_dir)
        frozen_bytes = _canonical_json_bytes(payload)
        frozen = json.loads(frozen_bytes)
        _validate_prototype_evidence_shape(frozen)
        with _workflow_lock(path):
            action = _validate_action_if_required(
                path, action_id, run_revision, "submit-prototype-evidence"
            )
            if action is not None:
                pose = next(
                    item for item in action["required_inputs"] if item["name"] == "pose_id"
                )
                if frozen["pose_id"] not in pose.get("allowed_values", ()):
                    raise ActionError("action request does not match")
            if _read_json(path / "workflow.json") != {
                "schema_version": 2, "state": "prototyping", "blocked": None,
            }:
                raise ValueError("prototype workflow is invalid")
            manifest = validated_prototype_manifest(path)
            metadata = _read_json(path / "omnipet-run.json")
            if (
                frozen["pet_id"] != metadata.get("pet_id")
                or frozen["design_revision"] != metadata.get("design_revision")
            ):
                raise ValueError("prototype evidence identity is invalid")
            matching = [job for job in manifest["jobs"] if job["id"] == frozen["pose_id"]]
            if len(matching) != 1:
                raise ValueError("prototype evidence pose is invalid")
            job = matching[0]
            if (
                job["status"] != "complete"
                or frozen["artifact"] != {
                    "path": job["output_path"],
                    "sha256": job["metadata"].get("source_sha256"),
                }
                or _hash_regular_file(path / job["output_path"]) != frozen["artifact"]["sha256"]
            ):
                raise ValueError("prototype evidence artifact is invalid")
            destination_dir = path / "qa/design-pack/prototypes"
            if destination_dir.is_symlink():
                raise ValueError("prototype evidence directory is unsafe")
            destination_dir.mkdir(exist_ok=True)
            destination = destination_dir / f"{job['id']}.json"
            previous = _optional_regular_bytes(destination)
            if previous is not None:
                if previous != frozen_bytes:
                    raise ValueError("prototype evidence already differs")
                return WorkflowState("prototyping")
            _write_bytes_atomic(destination, frozen_bytes)
            return WorkflowState("prototyping")
    except ActionError:
        raise
    except Exception:
        raise DesignPackError("prototype evidence submission failed") from None


def submit_design_pack_summary(
    run_dir: Path, contact_sheet_path: Path, review_payload: Any,
    *, action_id: str | None = None, run_revision: str | None = None,
) -> WorkflowState:
    try:
        path = _validated_run_dir(run_dir)
        contact_source = Path(contact_sheet_path).absolute()
        contact_bytes = _read_regular_bytes(contact_source)
        _validate_png_bytes(contact_bytes)
        review_bytes = _canonical_json_bytes(review_payload)
        review = json.loads(review_bytes)
        _validate_summary_shape(review)
        targets = (
            path / "qa/design-pack/contact-sheet.png",
            path / "qa/design-pack/review.json",
            path / "design/prototype-jobs-approved.json",
            path / "design/design-pack.json",
            path / "workflow.json",
        )
        with _workflow_lock(path):
            _validate_action_if_required(
                path, action_id, run_revision, "submit-design-pack-summary"
            )
            before = {target: _optional_regular_bytes(target) for target in targets}
            try:
                manifest = validated_prototype_manifest(path)
                if any(job["status"] != "complete" for job in manifest["jobs"]):
                    raise ValueError("prototype jobs are incomplete")
                intake = _read_json(path / "design/intake.json")
                plan = _read_json(path / "design/prototype-plan.json")
                prototype_reviews, warnings = _validated_prototype_reviews(path, manifest)
                expected_evidence = {
                    "contact_sheet_sha256": _sha256_bytes(contact_bytes),
                    "prototype_reviews": [
                        {"path": relative, "sha256": digest}
                        for relative, digest in sorted(prototype_reviews.items())
                    ],
                }
                if (
                    review["accepted_warnings"] != warnings
                    or review["expected_provider_calls"] != plan["estimated_provider_calls"]
                    or review["budget_authorized_usd"] != intake["budget"]["authorized_usd"]
                    or review["evidence"] != expected_evidence
                ):
                    raise ValueError("design pack review does not match evidence")
                _write_bytes_atomic(targets[0], contact_bytes)
                _write_bytes_atomic(targets[1], review_bytes)
                _write_bytes_atomic(targets[2], _canonical_json_bytes(manifest))
                if (
                    _read_json(targets[2]) != manifest
                    or validated_prototype_manifest(path) != manifest
                ):
                    raise ValueError("prototype manifest snapshot changed")
                pack = _build_design_pack(path, manifest, intake, warnings)
                _validate_design_pack_manifest(pack)
                _write_bytes_atomic(targets[3], _canonical_json_bytes(pack))
                context = _pin_directories(path)
                try:
                    result = _transition_workflow_pinned(context, "prototypes-passed")
                finally:
                    context.close()
                return result
            except Exception:
                for target, content in before.items():
                    if content is None:
                        if target.is_symlink():
                            raise
                        target.unlink(missing_ok=True)
                    else:
                        _write_bytes_atomic(target, content)
                raise
    except ActionError:
        raise
    except Exception:
        raise DesignPackError("design pack summary submission failed") from None


def _validate_prototype_evidence_shape(value: Any) -> None:
    if (
        not isinstance(value, dict) or set(value) != _PROTOTYPE_EVIDENCE_KEYS
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or not isinstance(value["artifact"], dict)
        or set(value["artifact"]) != {"path", "sha256"}
        or not isinstance(value["verdicts"], dict)
        or set(value["verdicts"]) != set(_VERDICT_CATEGORIES)
    ):
        raise ValueError("prototype evidence is invalid")
    _validate_text(value["pet_id"]); _validate_text(value["design_revision"])
    if _PET_ID.fullmatch(value["pet_id"]) is None or _DESIGN_REVISION.fullmatch(value["design_revision"]) is None:
        raise ValueError("prototype evidence is invalid")
    if _PET_ID.fullmatch(value["pose_id"]) is None:
        raise ValueError("prototype evidence is invalid")
    _safe_relative_path(value["artifact"]["path"]); _validate_hash(value["artifact"]["sha256"])
    warnings = []
    for category in _VERDICT_CATEGORIES:
        verdict = value["verdicts"][category]
        if (
            not isinstance(verdict, dict) or set(verdict) != _VERDICT_KEYS
            or verdict["decision"] not in {"pass", "warning", "fail"}
            or verdict["decision"] == "fail"
            or verdict["reviewer_role"] not in (
                {"deterministic"} if category == "structural"
                else {"production-agent", "independent-visual-reviewer"}
            )
        ):
            raise ValueError("prototype evidence is invalid")
        _validate_text(verdict["reviewer_principal_id"])
        criteria = _validated_text_list(verdict["criteria"], nonempty=True)
        if verdict["decision"] == "warning":
            warnings.extend(criteria)
    accepted = _validated_text_list(value["accepted_warnings"])
    if accepted != warnings:
        raise ValueError("prototype warnings are invalid")


def _validate_summary_shape(value: Any) -> None:
    if (
        not isinstance(value, dict) or set(value) != _SUMMARY_KEYS
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["decision"] != "pass"
        or type(value["expected_provider_calls"]) is not int
        or value["expected_provider_calls"] <= 0
        or type(value["budget_authorized_usd"]) not in {int, float}
        or not math.isfinite(value["budget_authorized_usd"])
        or value["budget_authorized_usd"] <= 0
    ):
        raise ValueError("design pack review is invalid")
    _validated_text_list(value["known_risks"])
    _validated_text_list(value["accepted_warnings"])
    _validate_text(value["reviewer_principal_id"])
    if not isinstance(value["reviewed_at"], str):
        raise ValueError("design pack review is invalid")
    timestamp = datetime.fromisoformat(value["reviewed_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
        raise ValueError("design pack review is invalid")
    evidence = value["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {"contact_sheet_sha256", "prototype_reviews"}:
        raise ValueError("design pack review is invalid")
    _validate_hash(evidence["contact_sheet_sha256"])
    if not isinstance(evidence["prototype_reviews"], list):
        raise ValueError("design pack review is invalid")
    paths = []
    for item in evidence["prototype_reviews"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("design pack review is invalid")
        paths.append(_safe_relative_path(item["path"])); _validate_hash(item["sha256"])
    if paths != sorted(set(paths)):
        raise ValueError("design pack review is invalid")


def _validated_text_list(value: Any, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or len(value) > 256:
        raise ValueError("design pack text list is invalid")
    for item in value:
        _validate_text(item)
    if len(value) != len(set(value)):
        raise ValueError("design pack text list is invalid")
    return value


def _validated_prototype_reviews(
    run_dir: Path, manifest: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    records = {}
    warnings = []
    for job in manifest["jobs"]:
        relative = f"qa/design-pack/prototypes/{job['id']}.json"
        review_path = run_dir / relative
        review = _read_json(review_path)
        _validate_prototype_evidence_shape(review)
        if (
            review["pose_id"] != job["id"]
            or review["design_revision"] != job["design_revision"]
            or review["artifact"] != {
                "path": job["output_path"], "sha256": job["metadata"]["source_sha256"],
            }
        ):
            raise ValueError("prototype review is stale")
        records[relative] = _hash_regular_file(review_path)
        for warning in review["accepted_warnings"]:
            if warning not in warnings:
                warnings.append(warning)
    return records, warnings


def _build_design_pack(
    run_dir: Path, job_manifest: dict[str, Any], intake: dict[str, Any], warnings: list[str],
) -> dict[str, Any]:
    paths = set(_DESIGN_PACK_PATHS)
    paths.add("design/prototype-jobs-approved.json")
    for job in job_manifest["jobs"]:
        paths.update((
            job["prompt_file"], job["output_path"],
            f"qa/design-pack/prototypes/{job['id']}.json",
        ))
    paths.update(("qa/design-pack/contact-sheet.png", "qa/design-pack/review.json"))
    artifacts = []
    for relative in sorted(paths):
        kind = (
            "prototype" if relative.startswith("decoded/")
            else "contact-sheet" if relative.endswith("contact-sheet.png")
            else "review" if relative.startswith("qa/")
            else "design"
        )
        artifacts.append({"path": relative, "sha256": _hash_regular_file(run_dir / relative), "kind": kind})
    return {
        "schema_version": 1,
        "pet_id": intake["pet_id"],
        "design_revision": intake["design_revision"],
        "artifacts": artifacts,
        "hashes": {
            "prototype_plan_sha256": _hash_regular_file(run_dir / "design/prototype-plan.json"),
            "contact_sheet_sha256": _hash_regular_file(run_dir / "qa/design-pack/contact-sheet.png"),
        },
        "schema_versions": {
            "intake": 1, "design_contract": 1, "state_storyboard": 1,
            "prototype_plan": 1, "look_mechanics": 1, "prototype_evidence": 1,
        },
        "budget_authorization": dict(intake["budget"]),
        "accepted_warnings": list(warnings),
        "owner_decisions": list(intake["owner_decisions"]),
    }


def _validate_design_pack_manifest(value: Any) -> None:
    keys = {
        "schema_version", "pet_id", "design_revision", "artifacts", "hashes",
        "schema_versions", "budget_authorization", "accepted_warnings", "owner_decisions",
    }
    if not isinstance(value, dict) or set(value) != keys or value["schema_version"] != 1:
        raise ValueError("design pack manifest is invalid")
    paths = []
    for item in value["artifacts"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "kind"} or item["kind"] not in {"design", "prototype", "review", "contact-sheet"}:
            raise ValueError("design pack manifest is invalid")
        paths.append(_safe_relative_path(item["path"])); _validate_hash(item["sha256"])
    if paths != sorted(set(paths)):
        raise ValueError("design pack manifest is invalid")
    if value["schema_versions"] != {
        "intake": 1, "design_contract": 1, "state_storyboard": 1,
        "prototype_plan": 1, "look_mechanics": 1, "prototype_evidence": 1,
    }:
        raise ValueError("design pack manifest is invalid")
    if set(value["hashes"]) != {"prototype_plan_sha256", "contact_sheet_sha256"}:
        raise ValueError("design pack manifest is invalid")
    for digest in value["hashes"].values(): _validate_hash(digest)
    if set(value["budget_authorization"]) != _BUDGET_KEYS:
        raise ValueError("design pack manifest is invalid")
    _validated_text_list(value["accepted_warnings"]); _validated_text_list(value["owner_decisions"])


def submit_design(
    run_dir: Path,
    *,
    contract: Any,
    rationale: Any,
    storyboard: Any,
    prototype_plan: Any,
    look_mechanics: Any,
    action_id: str | None = None,
    run_revision: str | None = None,
) -> WorkflowState:
    context = None
    try:
        path = _validated_run_dir(run_dir)
        context = _pin_directories(path)
        values = {
            "contract": contract,
            "rationale": rationale,
            "storyboard": storyboard,
            "prototype_plan": prototype_plan,
            "look_mechanics": look_mechanics,
        }
        frozen_bytes = {
            name: (value.encode("utf-8") if name == "rationale" and isinstance(value, str) else _canonical_json_bytes(value))
            for name, value in values.items()
        }
        frozen = {
            name: (content.decode("utf-8") if name == "rationale" else json.loads(content))
            for name, content in frozen_bytes.items()
        }
        _validate_design_submission_pinned(context, frozen)
        with _workflow_lock_pinned(context):
            _validate_action_if_required(
                path, action_id, run_revision, "submit-design"
            )
            _recover_design_submission_pinned(context)
            current_bytes = {
                name: (value.encode("utf-8") if name == "rationale" and isinstance(value, str) else _canonical_json_bytes(value))
                for name, value in values.items()
            }
            if current_bytes != frozen_bytes:
                raise ValueError("design changed during submission")
            _validate_design_submission_pinned(context, frozen)
            workflow_before = _read_pinned(context, "run", "workflow.json")
            metadata = json.loads(_read_pinned(context, "run", "omnipet-run.json"))
            artifact_content = {
                "omnipet-run.json": _read_pinned(context, "run", "omnipet-run.json"),
                "design/intake.json": _read_pinned(context, "design", "intake.json"),
                **{f"design/{filename}": frozen_bytes[name] for filename, name in _DESIGN_FILES.items()},
            }
            artifact_hashes = {
                relative: _sha256_bytes(content) for relative, content in artifact_content.items()
            }
            prototype_content = build_prototype_publication(
                frozen["contract"], frozen["prototype_plan"], metadata,
                frozen_bytes["prototype_plan"], artifact_hashes,
            )
            publication = {
                **{f"design/{filename}": frozen_bytes[name] for filename, name in _DESIGN_FILES.items()},
                **prototype_content,
            }
            journal = _prepare_design_publication(context, publication, workflow_before)
            try:
                _create_design_directories(context)
                for relative, content in publication.items():
                    _write_publication_file(context, relative, content)
                result = _transition_workflow_pinned(context, "contracts-validated")
                journal = {**journal, "state": "installed"}
                _write_pinned(context, "run", _DESIGN_JOURNAL, _canonical_json_bytes(journal))
                _finalize_design_publication(context, journal)
                return result
            except Exception:
                _recover_design_submission_pinned(context)
                raise
    except ActionError:
        raise
    except Exception:
        raise DesignPackError("design submission failed") from None
    finally:
        if context is not None:
            context.close()


def _validate_design_submission_pinned(context: _PinnedDirs, values: dict[str, Any]) -> None:
    metadata = json.loads(_read_pinned(context, "run", "omnipet-run.json").decode("utf-8"))
    intake = json.loads(_read_pinned(context, "design", "intake.json").decode("utf-8"))
    _validate_payload_shape(intake)
    if (
        _workflow_state_pinned(context).state != "designing"
        or not isinstance(metadata, dict)
        or metadata.get("schema_version") != 2
        or intake.get("pet_id") != metadata.get("pet_id")
        or intake.get("design_revision") != metadata.get("design_revision")
    ):
        raise ValueError("design identity is invalid")
    _validate_current_design_run_pinned(context, intake, metadata)
    validate_design_documents(
        values["contract"], values["rationale"], values["storyboard"],
        values["prototype_plan"], values["look_mechanics"],
        pet_id=metadata["pet_id"], design_revision=metadata["design_revision"],
        intake=intake,
    )


def _validate_current_design_run_pinned(
    context: _PinnedDirs,
    intake: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    records = metadata.get("references")
    if not isinstance(records, list):
        raise ValueError("design identity is invalid")
    expected = []
    for record in records:
        if not isinstance(record, dict) or set(record) != _METADATA_REFERENCE_KEYS:
            raise ValueError("design identity is invalid")
        relative = _safe_relative_path(record["run_path"])
        if not relative.startswith("references/"):
            raise ValueError("design identity is invalid")
        filename = Path(relative).relative_to("references").as_posix()
        if _hash_pinned(context, "references", filename) != record["sha256"]:
            raise ValueError("design identity is invalid")
        expected.append({"path": relative, "role": record["role"], "sha256": record["sha256"]})
    if intake["references"] != expected:
        raise ValueError("design identity is invalid")


def _prepare_design_publication(
    context: _PinnedDirs,
    content: dict[str, bytes],
    workflow_before: bytes,
) -> dict[str, Any]:
    if any((context.run_dir / relative).exists() or (context.run_dir / relative).is_symlink() for relative in _DESIGN_DIRECTORIES):
        raise ValueError("design publication directory already exists")
    backup_names = [_design_backup_name(relative) for relative in content]
    backup_names.append(".design-backup-workflow.json")
    for filename in (_DESIGN_JOURNAL, _DESIGN_PREPARE, *backup_names):
        if _read_pinned(context, "run", filename, optional=True) is not None:
            raise ValueError("stale design recovery evidence")
    marker = {
        "schema_version": 1,
        "backups": backup_names,
    }
    _write_pinned(context, "run", _DESIGN_PREPARE, _canonical_json_bytes(marker))
    files = []
    for relative, installed in content.items():
        previous = _read_publication_file(context, relative, optional=True)
        backup = _design_backup_name(relative)
        if previous is not None:
            _write_pinned(context, "run", backup, previous)
        files.append({
            "path": relative,
            "backup": backup,
            "previous": _sha256_bytes(previous) if previous is not None else None,
            "installed": _sha256_bytes(installed),
        })
    workflow_backup = ".design-backup-workflow.json"
    _write_pinned(context, "run", workflow_backup, workflow_before)
    journal = {
        "schema_version": 1,
        "state": "prepared",
        "files": files,
        "directories": list(_DESIGN_DIRECTORIES),
        "workflow": {
            "backup": workflow_backup,
            "previous": _sha256_bytes(workflow_before),
            "installed": _sha256_bytes(_canonical_json_bytes({"schema_version": 2, "state": "prototyping", "blocked": None})),
        },
    }
    _validate_design_journal(journal)
    _write_pinned(context, "run", _DESIGN_JOURNAL, _canonical_json_bytes(journal))
    _unlink_pinned(context, "run", _DESIGN_PREPARE)
    return journal


def _validate_design_journal(value: Any) -> None:
    if (
        not isinstance(value, dict) or set(value) != _DESIGN_JOURNAL_KEYS
        or value["schema_version"] != 1 or value["state"] not in {"prepared", "installed", "rolled-back"}
        or not isinstance(value["files"], list)
        or not value["files"]
        or value["directories"] != list(_DESIGN_DIRECTORIES)
    ):
        raise ValueError("design journal is invalid")
    paths = [item.get("path") for item in value["files"] if isinstance(item, dict)]
    required = [f"design/{filename}" for filename in _DESIGN_FILES]
    if paths[:len(required)] != required or paths[-1:] != ["imagegen-jobs.json"] or len(paths) != len(set(paths)):
        raise ValueError("design journal is invalid")
    for item in value["files"]:
        if set(item) != {"path", "backup", "previous", "installed"} or item["backup"] != _design_backup_name(item["path"]):
            raise ValueError("design journal is invalid")
        _validate_hash(item["installed"])
        if item["previous"] is not None: _validate_hash(item["previous"])
    workflow = value["workflow"]
    if not isinstance(workflow, dict) or set(workflow) != {"backup", "previous", "installed"} or workflow["backup"] != ".design-backup-workflow.json":
        raise ValueError("design journal is invalid")
    _validate_hash(workflow["previous"]); _validate_hash(workflow["installed"])


def _recover_design_submission_pinned(context: _PinnedDirs) -> None:
    content = _read_pinned(context, "run", _DESIGN_JOURNAL, optional=True)
    if content is None:
        marker = _read_pinned(context, "run", _DESIGN_PREPARE, optional=True)
        if marker is not None:
            value = json.loads(marker.decode("utf-8"))
            if (
                not isinstance(value, dict) or set(value) != {"schema_version", "backups"}
                or value["schema_version"] != 1 or not isinstance(value["backups"], list)
                or not all(isinstance(item, str) and item.startswith(".design-backup-") for item in value["backups"])
                or value["backups"][-1:] != [".design-backup-workflow.json"]
            ):
                raise ValueError("design preparation marker is invalid")
            for backup in value["backups"]:
                _unlink_pinned(context, "run", backup)
            _unlink_pinned(context, "run", _DESIGN_PREPARE)
        return
    journal = json.loads(content.decode("utf-8")); _validate_design_journal(journal)
    if journal["state"] == "rolled-back":
        _verify_design_rollback(context, journal)
        _finalize_design_publication(context, journal, validate=False)
        return
    installed = journal["state"] == "installed"
    if installed:
        try:
            installed = _sha256_bytes(_read_pinned(context, "run", "workflow.json")) == journal["workflow"]["installed"] and all(
                _sha256_bytes(_read_publication_file(context, item["path"])) == item["installed"]
                for item in journal["files"]
            )
        except Exception:
            installed = False
    if installed:
        _finalize_design_publication(context, journal)
        return
    for item in journal["files"]:
        if item["previous"] is None:
            _unlink_publication_file(context, item["path"])
        else:
            backup = _read_pinned(context, "run", item["backup"])
            if _sha256_bytes(backup) != item["previous"]: raise ValueError("design backup is invalid")
            _write_publication_file(context, item["path"], backup)
    workflow = _read_pinned(context, "run", journal["workflow"]["backup"])
    if _sha256_bytes(workflow) != journal["workflow"]["previous"]: raise ValueError("design backup is invalid")
    _write_pinned(context, "run", "workflow.json", workflow)
    _remove_design_directories(context)
    journal = {**journal, "state": "rolled-back"}
    _write_pinned(context, "run", _DESIGN_JOURNAL, _canonical_json_bytes(journal))
    _verify_design_rollback(context, journal)
    _finalize_design_publication(context, journal, validate=False)


def _verify_design_rollback(context: _PinnedDirs, journal: dict[str, Any]) -> None:
    if _sha256_bytes(_read_pinned(context, "run", "workflow.json")) != journal["workflow"]["previous"]:
        raise ValueError("design rollback is invalid")
    for item in journal["files"]:
        content = _read_publication_file(context, item["path"], optional=True)
        actual = _sha256_bytes(content) if content is not None else None
        if actual != item["previous"]:
            raise ValueError("design rollback is invalid")


def _finalize_design_publication(context: _PinnedDirs, journal: dict[str, Any], *, validate: bool = True) -> None:
    if validate and journal["state"] != "installed":
        raise ValueError("design publication is incomplete")
    for item in journal["files"]:
        _unlink_pinned(context, "run", item["backup"])
    _unlink_pinned(context, "run", journal["workflow"]["backup"])
    _unlink_pinned(context, "run", _DESIGN_PREPARE)
    _unlink_pinned(context, "run", _DESIGN_JOURNAL)


def _create_design_directories(context: _PinnedDirs) -> None:
    for relative in _DESIGN_DIRECTORIES:
        path = context.run_dir / relative
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("design publication directory is unsafe")
        path.mkdir(exist_ok=True)
        _fsync_directory(path.parent)


def _remove_design_directories(context: _PinnedDirs) -> None:
    for relative in reversed(_DESIGN_DIRECTORIES):
        path = context.run_dir / relative
        if path.is_symlink():
            raise ValueError("design publication directory is unsafe")
        try:
            path.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            if any(path.iterdir()):
                raise ValueError("design publication directory is not empty") from None


def _design_backup_name(relative: str) -> str:
    _safe_relative_path(relative)
    if relative.startswith("design/") and "/" not in relative.removeprefix("design/"):
        return f".design-backup-{relative.removeprefix('design/')}"
    return f".design-backup-{relative.replace('/', '--')}"


def _publication_location(relative: str) -> tuple[str, str]:
    value = _safe_relative_path(relative)
    if value.startswith("design/"):
        filename = value.removeprefix("design/")
        if "/" in filename:
            raise ValueError("design publication path is invalid")
        return "design", filename
    if value == "imagegen-jobs.json":
        return "run", value
    if value.startswith("prompts/prototypes/") and value.endswith(".md"):
        return "path", value
    raise ValueError("design publication path is invalid")


def _read_publication_file(
    context: _PinnedDirs, relative: str, *, optional: bool = False
) -> bytes | None:
    location, filename = _publication_location(relative)
    if location != "path":
        return _read_pinned(context, location, filename, optional=optional)
    path = context.run_dir / filename
    try:
        return _read_regular_bytes(path)
    except FileNotFoundError:
        if optional:
            return None
        raise


def _write_publication_file(context: _PinnedDirs, relative: str, content: bytes) -> None:
    location, filename = _publication_location(relative)
    if location != "path":
        _write_pinned(context, location, filename, content)
        return
    parent = (context.run_dir / filename).parent
    if parent.is_symlink() or not parent.is_dir() or not parent.resolve().is_relative_to(context.run_dir):
        raise ValueError("design publication path is unsafe")
    _write_bytes_atomic(context.run_dir / filename, content)


def _unlink_publication_file(context: _PinnedDirs, relative: str) -> None:
    location, filename = _publication_location(relative)
    if location != "path":
        _unlink_pinned(context, location, filename)
        return
    path = context.run_dir / filename
    if path.is_symlink():
        raise ValueError("design publication path is unsafe")
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def submit_intake(
    run_dir: Path, payload: Any, *, action_id: str | None = None,
    run_revision: str | None = None,
) -> WorkflowState:
    context = None
    try:
        path = _validated_run_dir(run_dir)
        context = _pin_directories(path)
        _validate_payload_shape(payload)
        frozen_bytes = _canonical_json_bytes(payload)
        frozen = json.loads(frozen_bytes)
        _validate_current_run_pinned(context, frozen)

        with _workflow_lock_pinned(context):
            _validate_action_if_required(
                path, action_id, run_revision, "submit-intake"
            )
            _recover_intake_submission_pinned(context)
            if _canonical_json_bytes(payload) != frozen_bytes:
                raise ValueError("intake changed during submission")
            _validate_payload_shape(frozen)
            _validate_current_run_pinned(context, frozen)
            workflow_before = _read_pinned(context, "run", "workflow.json")
            intake_before = _read_pinned(
                context, "design", "intake.json", optional=True
            )
            journal = _prepare_publication(
                context, frozen_bytes, workflow_before, intake_before
            )
            try:
                _write_pinned(context, "design", "intake.json", frozen_bytes)
                journal = _update_journal_state(
                    context, journal, "intake-installed"
                )
                result = _transition_workflow_pinned(context, "intake-validated")
                journal = _update_journal_state(
                    context, journal, "workflow-installed"
                )
                _finalize_publication(context, journal)
                return result
            except Exception:
                _recover_intake_submission_pinned(context)
                raise
    except ActionError:
        raise
    except Exception:
        raise DesignPackError("intake submission failed") from None
    finally:
        if context is not None:
            context.close()


def _validate_payload_shape(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != _INTAKE_KEYS:
        raise ValueError("intake is invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("intake is invalid")
    _validate_text(payload["pet_id"])
    _validate_text(payload["design_revision"])
    if _PET_ID.fullmatch(payload["pet_id"]) is None:
        raise ValueError("intake is invalid")
    if _DESIGN_REVISION.fullmatch(payload["design_revision"]) is None:
        raise ValueError("intake is invalid")
    _validate_text(payload["style_request"])

    references = payload["references"]
    if not isinstance(references, list) or len(references) > 64:
        raise ValueError("intake is invalid")
    seen_references: set[str] = set()
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != _REFERENCE_KEYS:
            raise ValueError("intake is invalid")
        path = _safe_relative_path(reference["path"])
        _validate_text(reference["role"])
        _validate_hash(reference["sha256"])
        if path in seen_references:
            raise ValueError("intake is invalid")
        seen_references.add(path)

    rights = payload["rights"]
    if (
        not isinstance(rights, dict)
        or set(rights) != _RIGHTS_KEYS
        or rights["status"] != "declared"
    ):
        raise ValueError("intake is invalid")
    _validate_text(rights["note"])

    budget = payload["budget"]
    if not isinstance(budget, dict) or set(budget) != _BUDGET_KEYS:
        raise ValueError("intake is invalid")
    authorized = budget["authorized_usd"]
    calls = budget["estimated_provider_calls"]
    if (
        type(authorized) not in {int, float}
        or not math.isfinite(authorized)
        or authorized <= 0
        or type(calls) is not int
        or calls <= 0
    ):
        raise ValueError("intake is invalid")

    seen_facts: set[str] = set()
    for field in _FACT_FIELDS:
        values = payload[field]
        if (
            not isinstance(values, list)
            or (field == "observed_facts" and not values)
            or len(values) > 256
        ):
            raise ValueError("intake is invalid")
        for value in values:
            _validate_text(value)
            if value in seen_facts:
                raise ValueError("intake is invalid")
            seen_facts.add(value)


def _validate_current_run_pinned(
    context: _PinnedDirs,
    payload: dict[str, Any],
) -> None:
    metadata = json.loads(
        _read_pinned(context, "run", "omnipet-run.json").decode("utf-8")
    )
    if (
        not isinstance(metadata, dict)
        or set(metadata) != _METADATA_KEYS
        or type(metadata["schema_version"]) is not int
        or metadata["schema_version"] != 2
        or payload["pet_id"] != metadata["pet_id"]
        or payload["design_revision"] != metadata["design_revision"]
    ):
        raise ValueError("run metadata is invalid")
    _validate_text(metadata["pet_id"])
    _validate_text(metadata["design_revision"])

    records = metadata["references"]
    if not isinstance(records, list):
        raise ValueError("run metadata is invalid")
    expected = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != _METADATA_REFERENCE_KEYS:
            raise ValueError("run metadata is invalid")
        relative = _safe_relative_path(record["run_path"])
        if not relative.startswith("references/") or relative in seen:
            raise ValueError("run metadata is invalid")
        seen.add(relative)
        _validate_text(record["role"])
        _validate_hash(record["sha256"])
        filename = Path(relative).relative_to("references").as_posix()
        if _hash_pinned(context, "references", filename) != record["sha256"]:
            raise ValueError("reference snapshot is invalid")
        expected.append({
            "path": relative,
            "role": record["role"],
            "sha256": record["sha256"],
        })
    if payload["references"] != expected:
        raise ValueError("intake references are invalid")

    workflow = _workflow_state_pinned(context)
    if workflow.state != "intake":
        raise ValueError("workflow state is invalid")
    _read_pinned(context, "design", "intake.json", optional=True)


def _workflow_state_pinned(context: _PinnedDirs) -> WorkflowState:
    data = json.loads(
        _read_pinned(context, "run", "workflow.json").decode("utf-8")
    )
    return _validate_workflow_v2(data)


def _transition_workflow_pinned(
    context: _PinnedDirs,
    event: str,
) -> WorkflowState:
    current = _workflow_state_pinned(context)
    next_state = PHASE2_TRANSITIONS.get((current.state, event))
    if next_state is None:
        raise ValueError("workflow transition is invalid")
    result = WorkflowState(next_state)
    _write_pinned(
        context,
        "run",
        "workflow.json",
        _canonical_json_bytes({
            "schema_version": 2,
            "state": result.state,
            "blocked": result.blocked,
        }),
    )
    return result


def _validated_run_dir(run_dir: Path) -> Path:
    path = Path(run_dir).absolute()
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ValueError("run directory is unsafe")
    return path


def _validate_text(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 10_000
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or contains_credential_like_text(value)
    ):
        raise ValueError("intake text is invalid")


def _validate_hash(value: Any) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("intake hash is invalid")


def _safe_relative_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or "\\" in value
    ):
        raise ValueError("intake path is invalid")
    path = Path(value)
    if path == Path(".") or path.is_absolute() or ".." in path.parts:
        raise ValueError("intake path is invalid")
    normalized = path.as_posix()
    if normalized != value or any(part in {"", "."} for part in path.parts):
        raise ValueError("intake path is invalid")
    return normalized


def _read_json(path: Path) -> Any:
    return json.loads(_read_regular_bytes(path).decode("utf-8"))


def _read_regular_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("artifact is unsafe")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _optional_regular_bytes(path: Path) -> bytes | None:
    try:
        return _read_regular_bytes(path)
    except FileNotFoundError:
        return None


def _hash_regular_file(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("reference snapshot is unsafe")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(path, _canonical_json_bytes(payload))


def _canonical_json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("artifact path is unsafe")
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


def recover_intake_submission(run_dir: Path) -> None:
    path = _validated_run_dir(run_dir)
    run_fd, run_identity = _pin_run_directory(path)
    try:
        if not _recovery_is_present(path, run_fd, run_identity):
            return
        context = _pin_remaining_directories(path, run_fd, run_identity)
        run_fd = -1
        with _workflow_lock_pinned(context):
            _recover_intake_submission_pinned(context)
            _recover_design_submission_pinned(context)
    finally:
        if "context" in locals():
            context.close()
        elif run_fd >= 0:
            os.close(run_fd)


def _recovery_is_present(
    run_dir: Path,
    descriptor: int,
    identity: tuple[int, int],
) -> bool:
    for filename in (_JOURNAL_NAME, _PREPARE_MARKER, _DESIGN_JOURNAL, _DESIGN_PREPARE):
        try:
            value = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(value.st_mode):
            raise ValueError("intake recovery marker is unsafe")
        _verify_directory_identity(run_dir, identity)
        return True
    _verify_directory_identity(run_dir, identity)
    return False


def _recover_intake_submission_unlocked(run_dir: Path) -> None:
    journal_path = run_dir / _JOURNAL_NAME
    if journal_path.is_symlink():
        raise ValueError("intake journal is unsafe")
    if not journal_path.exists():
        return
    context = _pin_directories(run_dir)
    try:
        _recover_intake_submission_pinned(context)
    finally:
        context.close()


def _recover_intake_submission_pinned(context: _PinnedDirs) -> None:
    content = _read_pinned(context, "run", _JOURNAL_NAME, optional=True)
    if content is None:
        _cleanup_owned_preparation(context)
        return
    journal = json.loads(content.decode("utf-8"))
    _validate_journal(journal)
    if journal["state"] == "workflow-installed" and _installed_pair_is_valid(
        context, journal
    ):
        _finalize_publication(context, journal)
        return
    if journal["state"] == "rollback-restored":
        _verify_rollback_restored(context, journal)
        _cleanup_publication(context)
        return
    _rollback_publication(context, journal)


def _prepare_publication(
    context: _PinnedDirs,
    intake_bytes: bytes,
    workflow_before: bytes,
    intake_before: bytes | None,
) -> dict[str, Any]:
    for filename in (
        _JOURNAL_NAME,
        _PREPARE_MARKER,
        _INTAKE_BACKUP,
        _INTAKE_ABSENT,
        _WORKFLOW_BACKUP,
    ):
        if _read_pinned(context, "run", filename, optional=True) is not None:
            raise ValueError("stale intake recovery evidence")
    preparation = {
        "schema_version": 1,
        "intake_previously_existed": intake_before is not None,
        "previous_intake_sha256": (
            _sha256_bytes(intake_before) if intake_before is not None else None
        ),
        "previous_workflow_sha256": _sha256_bytes(workflow_before),
    }
    _write_pinned(
        context,
        "run",
        _PREPARE_MARKER,
        _canonical_json_bytes(preparation),
    )
    _prepare_step(context, "preparation-owned")
    if intake_before is None:
        _write_pinned(context, "run", _INTAKE_ABSENT, b"")
    else:
        _write_pinned(context, "run", _INTAKE_BACKUP, intake_before)
    _prepare_step(context, "intake-backup-written")
    _write_pinned(context, "run", _WORKFLOW_BACKUP, workflow_before)
    _prepare_step(context, "workflow-backup-written")
    os.fsync(context.run_fd)
    _prepare_step(context, "backups-fsynced")
    installed_workflow = _canonical_json_bytes({
        "schema_version": 2,
        "state": "designing",
        "blocked": None,
    })
    journal = {
        "schema_version": 1,
        "state": "prepared",
        "intake_path": "design/intake.json",
        "workflow_path": "workflow.json",
        "intake_backup_path": _INTAKE_BACKUP,
        "intake_absent_path": _INTAKE_ABSENT,
        "workflow_backup_path": _WORKFLOW_BACKUP,
        "intake_previously_existed": intake_before is not None,
        "previous_intake_sha256": (
            _sha256_bytes(intake_before) if intake_before is not None else None
        ),
        "previous_workflow_sha256": _sha256_bytes(workflow_before),
        "installed_intake_sha256": _sha256_bytes(intake_bytes),
        "installed_workflow_sha256": _sha256_bytes(installed_workflow),
    }
    _write_pinned(context, "run", _JOURNAL_NAME, _canonical_json_bytes(journal))
    _unlink_pinned(context, "run", _PREPARE_MARKER)
    return journal


def _prepare_step(context: _PinnedDirs, step: str) -> None:
    _verify_pinned(context)


def _cleanup_owned_preparation(context: _PinnedDirs) -> None:
    content = _read_pinned(context, "run", _PREPARE_MARKER, optional=True)
    if content is None:
        return
    value = json.loads(content.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema_version",
            "intake_previously_existed",
            "previous_intake_sha256",
            "previous_workflow_sha256",
        }
        or value["schema_version"] != 1
        or type(value["intake_previously_existed"]) is not bool
    ):
        raise ValueError("intake preparation marker is invalid")
    _validate_hash(value["previous_workflow_sha256"])
    if value["intake_previously_existed"]:
        _validate_hash(value["previous_intake_sha256"])
    elif value["previous_intake_sha256"] is not None:
        raise ValueError("intake preparation marker is invalid")
    for filename in (_INTAKE_BACKUP, _INTAKE_ABSENT, _WORKFLOW_BACKUP):
        _unlink_pinned(context, "run", filename)
    _unlink_pinned(context, "run", _PREPARE_MARKER)


def _update_journal_state(
    context: _PinnedDirs,
    journal: dict[str, Any],
    state: str,
) -> dict[str, Any]:
    updated = {**journal, "state": state}
    _validate_journal(updated)
    _write_pinned(context, "run", _JOURNAL_NAME, _canonical_json_bytes(updated))
    return updated


def _validate_journal(journal: Any) -> None:
    if (
        not isinstance(journal, dict)
        or set(journal) != _JOURNAL_KEYS
        or type(journal["schema_version"]) is not int
        or journal["schema_version"] != 1
        or journal["state"] not in _JOURNAL_STATES
        or journal["intake_path"] != "design/intake.json"
        or journal["workflow_path"] != "workflow.json"
        or journal["intake_backup_path"] != _INTAKE_BACKUP
        or journal["intake_absent_path"] != _INTAKE_ABSENT
        or journal["workflow_backup_path"] != _WORKFLOW_BACKUP
        or type(journal["intake_previously_existed"]) is not bool
    ):
        raise ValueError("intake journal is invalid")
    for key in (
        "previous_workflow_sha256",
        "installed_intake_sha256",
        "installed_workflow_sha256",
    ):
        _validate_hash(journal[key])
    previous_intake = journal["previous_intake_sha256"]
    if journal["intake_previously_existed"]:
        _validate_hash(previous_intake)
    elif previous_intake is not None:
        raise ValueError("intake journal is invalid")


def _installed_pair_is_valid(context: _PinnedDirs, journal: dict[str, Any]) -> bool:
    try:
        return (
            _sha256_bytes(_read_pinned(context, "design", "intake.json"))
            == journal["installed_intake_sha256"]
            and _sha256_bytes(_read_pinned(context, "run", "workflow.json"))
            == journal["installed_workflow_sha256"]
        )
    except Exception:
        return False


def _rollback_publication(context: _PinnedDirs, journal: dict[str, Any]) -> None:
    if journal["intake_previously_existed"]:
        _restore_from_backup(
            context,
            journal["intake_backup_path"],
            "design",
            "intake.json",
            journal["previous_intake_sha256"],
        )
    else:
        if _read_pinned(context, "run", journal["intake_absent_path"]) != b"":
            raise ValueError("intake backup is invalid")
        _unlink_pinned(context, "design", "intake.json")
    _restore_from_backup(
        context,
        journal["workflow_backup_path"],
        "run",
        "workflow.json",
        journal["previous_workflow_sha256"],
    )
    journal = _update_journal_state(context, journal, "rollback-restored")
    _verify_rollback_restored(context, journal)
    _cleanup_publication(context)


def _restore_from_backup(
    context: _PinnedDirs,
    backup: str,
    location: str,
    target: str,
    expected_hash: str,
) -> None:
    content = _read_pinned(context, "run", backup)
    if _sha256_bytes(content) != expected_hash:
        raise ValueError("intake backup is invalid")
    _write_pinned(context, location, target, content)


def _finalize_publication(
    context: _PinnedDirs,
    journal: dict[str, Any],
    *,
    validate_pair: bool = True,
) -> None:
    if validate_pair and not _installed_pair_is_valid(context, journal):
        raise ValueError("installed intake pair is invalid")
    _cleanup_publication(context)


def _verify_rollback_restored(context: _PinnedDirs, journal: dict[str, Any]) -> None:
    workflow = _read_pinned(context, "run", "workflow.json")
    if _sha256_bytes(workflow) != journal["previous_workflow_sha256"]:
        raise ValueError("workflow restoration is invalid")
    intake = _read_pinned(context, "design", "intake.json", optional=True)
    if journal["intake_previously_existed"]:
        if intake is None or _sha256_bytes(intake) != journal["previous_intake_sha256"]:
            raise ValueError("intake restoration is invalid")
    elif intake is not None:
        raise ValueError("intake restoration is invalid")


def _cleanup_publication(context: _PinnedDirs) -> None:
    for filename in (
        _INTAKE_BACKUP,
        _INTAKE_ABSENT,
        _WORKFLOW_BACKUP,
        _PREPARE_MARKER,
        _JOURNAL_NAME,
    ):
        _unlink_recovery_file(context, filename)


def _unlink_recovery_file(context: _PinnedDirs, filename: str) -> None:
    _unlink_pinned(context, "run", filename)


def _pin_directories(run_dir: Path) -> _PinnedDirs:
    run_fd, run_identity = _pin_run_directory(run_dir)
    try:
        return _pin_remaining_directories(run_dir, run_fd, run_identity)
    except Exception:
        os.close(run_fd)
        raise


def _pin_run_directory(run_dir: Path) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    run_fd = os.open(run_dir, flags)
    value = os.fstat(run_fd)
    identity = (value.st_dev, value.st_ino)
    _verify_directory_identity(run_dir, identity)
    return run_fd, identity


def _pin_remaining_directories(
    run_dir: Path,
    run_fd: int,
    run_identity: tuple[int, int],
) -> _PinnedDirs:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    _verify_directory_identity(run_dir, run_identity)
    try:
        design_fd = os.open("design", flags, dir_fd=run_fd)
        references_fd = os.open("references", flags, dir_fd=run_fd)
    except Exception:
        if "design_fd" in locals():
            os.close(design_fd)
        raise
    design_stat = os.fstat(design_fd)
    references_stat = os.fstat(references_fd)
    context = _PinnedDirs(
        run_dir=run_dir,
        design_dir=run_dir / "design",
        run_fd=run_fd,
        design_fd=design_fd,
        references_fd=references_fd,
        run_identity=run_identity,
        design_identity=(design_stat.st_dev, design_stat.st_ino),
        references_identity=(references_stat.st_dev, references_stat.st_ino),
    )
    _verify_pinned(context)
    return context


def _verify_directory_identity(path: Path, identity: tuple[int, int]) -> None:
    value = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not stat.S_ISDIR(value.st_mode)
        or (value.st_dev, value.st_ino) != identity
    ):
        raise ValueError("intake directory changed")


@contextmanager
def _workflow_lock_pinned(context: _PinnedDirs):
    _verify_pinned(context)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(".workflow.lock", flags, 0o600, dir_fd=context.run_fd)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(context.run_fd)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _validate_lock_identity(descriptor, directory_fd=context.run_fd)
        _verify_pinned(context)
        yield
    finally:
        try:
            _verify_pinned(context)
            _validate_lock_identity(descriptor, directory_fd=context.run_fd)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _verify_pinned(context: _PinnedDirs) -> None:
    for path, identity in (
        (context.run_dir, context.run_identity),
        (context.design_dir, context.design_identity),
        (context.run_dir / "references", context.references_identity),
    ):
        value = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != identity:
            raise ValueError("intake directory changed")


def _descriptor(context: _PinnedDirs, location: str) -> int:
    return {
        "run": context.run_fd,
        "design": context.design_fd,
        "references": context.references_fd,
    }[location]


def _hash_pinned(context: _PinnedDirs, location: str, filename: str) -> str:
    return _sha256_bytes(_read_pinned(context, location, filename))


def _read_pinned(
    context: _PinnedDirs,
    location: str,
    filename: str,
    *,
    optional: bool = False,
) -> bytes | None:
    _verify_pinned(context)
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=_descriptor(context, location),
        )
    except FileNotFoundError:
        if optional:
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("artifact is unsafe")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_pinned(
    context: _PinnedDirs,
    location: str,
    filename: str,
    content: bytes,
) -> None:
    _verify_pinned(context)
    directory_fd = _descriptor(context, location)
    temporary = f".{filename}.tmp-{os.getpid()}-{id(content)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _verify_pinned(context)
        os.replace(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        _verify_pinned(context)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _unlink_pinned(context: _PinnedDirs, location: str, filename: str) -> None:
    _verify_pinned(context)
    directory_fd = _descriptor(context, location)
    try:
        os.unlink(filename, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    os.fsync(directory_fd)
    _verify_pinned(context)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
