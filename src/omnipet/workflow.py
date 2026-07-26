from __future__ import annotations

import json
import fcntl
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnipet.approvals import (
    STAGES,
    ApprovalError,
    _approve_stage_unlocked,
    invalidate_stale_approvals,
    load_approvals,
    required_artifacts,
)
from omnipet.diagnostics import SafeDiagnostic
from omnipet.security import contains_credential_like_text


STATES = (
    "preparing",
    "awaiting_base_approval",
    "generating_standard_rows",
    "awaiting_standard_rows_approval",
    "generating_directions",
    "awaiting_directions_approval",
    "building_package",
    "awaiting_package_approval",
    "complete",
    "blocked",
)
_DOCUMENT_KEYS = {"schema_version", "state", "blocked"}
_BLOCKED_KEYS = {"code", "job", "evidence"}


class WorkflowError(RuntimeError):
    """Raised when persisted workflow state or an operation is invalid."""


@dataclass(frozen=True)
class WorkflowState:
    state: str
    blocked: dict[str, Any] | None = None


def load_workflow(run_dir: Path) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    path = run_dir / "workflow.json"
    if not path.exists():
        return WorkflowState("preparing")
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("workflow document is unsafe")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != _DOCUMENT_KEYS or data.get("schema_version") != 1:
            raise ValueError("workflow schema is invalid")
        state, blocked = data.get("state"), data.get("blocked")
        if state not in STATES:
            raise ValueError("workflow state is invalid")
        if blocked is not None:
            _validate_blocked(blocked)
        if (state == "blocked") != (blocked is not None):
            raise ValueError("workflow blocked state is inconsistent")
        return WorkflowState(state, blocked)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise WorkflowError("workflow is invalid") from None


def refresh_workflow(run_dir: Path) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    with _workflow_lock(run_dir):
        return _refresh_workflow_unlocked(run_dir)


def _refresh_workflow_unlocked(run_dir: Path) -> WorkflowState:
    current = load_workflow(run_dir)
    if current.blocked is not None:
        return current
    try:
        had_package_approval = any(
            record.stage == "package" for record in load_approvals(run_dir)
        )
        approvals = invalidate_stale_approvals(run_dir)
        approved = {record.stage for record in approvals}
        jobs = _jobs(run_dir)
        failed = next((job for job in jobs if job.get("status") == "failed"), None)
        if failed is not None:
            result = WorkflowState(
                "blocked",
                {
                    "code": "job-failed", "job": failed["id"],
                    "evidence": None, "diagnostic": None,
                },
            )
            _write_workflow_unlocked(run_dir, result)
            return result
        complete = {job["id"] for job in jobs if job.get("status") == "complete"}
        standard = set(_job_ids()[1:10])
        directions = set(_job_ids()[10:])
        candidate = run_dir / "qa" / "candidates" / "base.json"
        if "base" not in complete and candidate.is_file() and not candidate.is_symlink():
            state = "awaiting_base_approval"
        elif "base" not in complete:
            state = "preparing"
        elif "base" not in approved and not _evidence_present(run_dir, "base"):
            state = "preparing"
        elif "base" not in approved:
            state = "awaiting_base_approval"
        elif not standard.issubset(complete):
            state = "generating_standard_rows"
        elif "standard-rows" not in approved and not _evidence_present(run_dir, "standard-rows"):
            state = "generating_standard_rows"
        elif "standard-rows" not in approved:
            state = "awaiting_standard_rows_approval"
        elif not directions.issubset(complete):
            state = "generating_directions"
        elif "directions" not in approved and not _evidence_present(run_dir, "directions"):
            state = "generating_directions"
        elif "directions" not in approved:
            state = "awaiting_directions_approval"
        elif (
            not had_package_approval
            and not _evidence_present(run_dir, "package")
        ):
            state = "building_package"
        elif "package" not in approved:
            state = "awaiting_package_approval"
        else:
            delivered = run_dir / "package-complete.json"
            if delivered.is_file() and not delivered.is_symlink():
                value = json.loads(delivered.read_text(encoding="utf-8"))
                expected = [item.path for item in next(record for record in approvals if record.stage == "package").artifacts]
                if value != {"schema_version": 1, "artifacts": expected}:
                    raise ValueError("package completion marker is invalid")
                state = "complete"
            else:
                state = "awaiting_package_approval"
        result = WorkflowState(state)
        _write_workflow_unlocked(run_dir, result)
        return result
    except (ApprovalError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise WorkflowError("workflow evidence is invalid") from None


def approve_workflow_stage(run_dir: Path, stage: str, *, note: str | None = None) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    with _workflow_lock(run_dir):
        return _approve_workflow_stage_unlocked(run_dir, stage, note=note)


def _approve_stage_operation(run_dir: Path, stage: str, *, note: str | None = None):
    run_dir = _validated_run_dir(run_dir)
    with _workflow_lock(run_dir):
        expected = _expected_approval_states()
        if stage not in STAGES or _refresh_workflow_unlocked(run_dir).state != expected[stage]:
            raise ApprovalError("workflow is not awaiting this approval")
        record = _approve_stage_unlocked(run_dir, stage, note=note)
        _refresh_workflow_unlocked(run_dir)
        return record


def _approve_workflow_stage_unlocked(run_dir: Path, stage: str, *, note: str | None) -> WorkflowState:
    expected = _expected_approval_states()
    if stage not in STAGES or _refresh_workflow_unlocked(run_dir).state != expected[stage]:
        raise WorkflowError("workflow is not awaiting this approval")
    try:
        _approve_stage_unlocked(run_dir, stage, note=note)
    except ApprovalError:
        raise WorkflowError("workflow approval failed") from None
    return _refresh_workflow_unlocked(run_dir)


def mark_blocked(
    run_dir: Path, *, code: str, job: str | None, evidence: str | None,
    diagnostic: SafeDiagnostic | None = None,
) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    with _workflow_lock(run_dir):
        return _mark_blocked_unlocked(
            run_dir, code=code, job=job, evidence=evidence, diagnostic=diagnostic
        )


def _mark_blocked_unlocked(
    run_dir: Path, *, code: str, job: str | None, evidence: str | None,
    diagnostic: SafeDiagnostic | None = None,
) -> WorkflowState:
    blocked = {
        "code": code,
        "job": job,
        "evidence": evidence,
        "diagnostic": diagnostic.to_dict() if diagnostic is not None else None,
    }
    try:
        _validate_blocked(blocked)
    except ValueError:
        raise WorkflowError("blocked record is invalid") from None
    state = WorkflowState("blocked", blocked)
    _write_workflow_unlocked(run_dir, state)
    return state


def clear_blocked(run_dir: Path) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    with _workflow_lock(run_dir):
        return _clear_blocked_unlocked(run_dir)


def _clear_blocked_unlocked(run_dir: Path) -> WorkflowState:
    current = load_workflow(run_dir)
    if current.blocked is None:
        raise WorkflowError("workflow is not blocked")
    _write_workflow_unlocked(run_dir, WorkflowState("preparing"))
    return _refresh_workflow_unlocked(run_dir)


def _jobs(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "imagegen-jobs.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("run manifest is unsafe")
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else None
    expected = _job_ids()
    if (
        not isinstance(jobs, list)
        or [job.get("id") for job in jobs if isinstance(job, dict)] != list(expected)
        or any(job.get("status") not in {"pending", "running", "complete", "failed"} for job in jobs)
    ):
        raise ValueError("run jobs are invalid")
    return jobs


def _job_ids() -> tuple[str, ...]:
    from omnipet.run import EXPECTED_JOB_IDS
    return EXPECTED_JOB_IDS


def _evidence_present(run_dir: Path, stage: str) -> bool:
    try:
        required_artifacts(run_dir, stage)
    except ApprovalError:
        return False
    return True


def _expected_approval_states() -> dict[str, str]:
    return {
        "base": "awaiting_base_approval",
        "standard-rows": "awaiting_standard_rows_approval",
        "directions": "awaiting_directions_approval",
        "package": "awaiting_package_approval",
    }


def mark_package_complete(run_dir: Path) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    with _workflow_lock(run_dir):
        if _refresh_workflow_unlocked(run_dir).state not in {"awaiting_package_approval", "complete"}:
            raise WorkflowError("package is not approved")
        approvals = invalidate_stale_approvals(run_dir)
        if not approvals or approvals[-1].stage != "package":
            raise WorkflowError("package is not approved")
        path = run_dir / "package-complete.json"
        descriptor, name = tempfile.mkstemp(prefix=".package-complete-", dir=run_dir)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump({"schema_version": 1, "artifacts": [item.path for item in approvals[-1].artifacts]}, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return _refresh_workflow_unlocked(run_dir)


def _validate_blocked(value: Any) -> None:
    if not isinstance(value, dict) or set(value) not in {
        frozenset(_BLOCKED_KEYS), frozenset(_BLOCKED_KEYS | {"diagnostic"})
    }:
        raise ValueError("blocked schema is invalid")
    code, job, evidence = value.get("code"), value.get("job"), value.get("evidence")
    if not isinstance(code, str) or not code.strip() or contains_credential_like_text(code):
        raise ValueError("blocked code is invalid")
    if job is not None and (not isinstance(job, str) or job not in _job_ids()):
        raise ValueError("blocked job is invalid")
    if evidence is not None:
        path = Path(evidence) if isinstance(evidence, str) else Path()
        if (
            not isinstance(evidence, str)
            or not evidence
            or path.is_absolute()
            or ".." in path.parts
            or contains_credential_like_text(evidence)
        ):
            raise ValueError("blocked evidence is invalid")
    if value.get("diagnostic") is not None:
        SafeDiagnostic.from_dict(value["diagnostic"])


def _write_workflow_unlocked(run_dir: Path, state: WorkflowState) -> None:
    payload = {"schema_version": 1, "state": state.state, "blocked": state.blocked}
    path = run_dir / "workflow.json"
    if path.is_symlink():
        raise WorkflowError("workflow path is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=".workflow.json-", dir=run_dir)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(run_dir)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_run_dir(run_dir: Path) -> Path:
    path = Path(run_dir).absolute()
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise WorkflowError("run directory is unsafe")
    return path


@contextmanager
def _workflow_lock(run_dir: Path):
    path = run_dir / ".workflow.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WorkflowError("workflow lock path is unsafe")
    existed = path.exists()
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise WorkflowError("workflow lock is unavailable") from None
    try:
        os.fchmod(descriptor, 0o600)
        if not existed:
            _fsync_directory(run_dir)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
