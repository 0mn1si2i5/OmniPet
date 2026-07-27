from __future__ import annotations

import json
import fcntl
import os
import stat
import tempfile
import threading
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
PHASE2_STATES = (
    "intake",
    "designing",
    "prototyping",
    "awaiting_design_pack_approval",
    "producing_standard_rows",
    "producing_directions",
    "building_package",
    "awaiting_package_approval",
    "complete",
    "blocked",
    "rejected",
)
PHASE2_TRANSITIONS = {
    ("intake", "intake-validated"): "designing",
    ("designing", "contracts-validated"): "prototyping",
    ("prototyping", "prototypes-passed"): "awaiting_design_pack_approval",
    ("awaiting_design_pack_approval", "design-pack-approved"): "producing_standard_rows",
}
_DOCUMENT_KEYS = {"schema_version", "state", "blocked"}
_BLOCKED_KEYS = {"code", "job", "evidence"}
_PHASE2_BLOCKED_KEYS = {
    "code",
    "prior_state",
    "job_id",
    "evidence_path",
    "root_failure_key",
    "recoveries",
    "diagnostic",
}
_LOCAL_WORKFLOW_LOCKS: dict[Path, threading.RLock] = {}
_LOCAL_WORKFLOW_LOCKS_GUARD = threading.Lock()


class WorkflowError(RuntimeError):
    """Raised when persisted workflow state or an operation is invalid."""


@dataclass(frozen=True)
class WorkflowState:
    state: str
    blocked: dict[str, Any] | None = None


def load_workflow(run_dir: Path) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    state, version = _load_workflow_unrecovered(run_dir)
    if version == 2:
        return load_workflow_v2(run_dir)
    return state


def _load_workflow_unrecovered(run_dir: Path) -> tuple[WorkflowState, int | None]:
    path = run_dir / "workflow.json"
    if not path.exists():
        return WorkflowState("preparing"), None
    try:
        data = _read_json_no_follow(path)
        if isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 2:
            return _validate_workflow_v2(data), 2
        if not isinstance(data, dict) or set(data) != _DOCUMENT_KEYS or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("workflow schema is invalid")
        state, blocked = data.get("state"), data.get("blocked")
        if state not in STATES:
            raise ValueError("workflow state is invalid")
        if blocked is not None:
            _validate_blocked(blocked)
        if (state == "blocked") != (blocked is not None):
            raise ValueError("workflow blocked state is inconsistent")
        return WorkflowState(state, blocked), 1
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise WorkflowError("workflow is invalid") from None


def load_workflow_v2(run_dir: Path) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    initial = _load_workflow_v2_unrecovered(run_dir)
    try:
        from omnipet.design_pack import recover_intake_submission
        from omnipet.prototype_jobs import recover_prototype_jobs

        recover_intake_submission(run_dir)
        recover_prototype_jobs(run_dir)
    except WorkflowError:
        raise
    except Exception:
        raise WorkflowError("workflow is invalid") from None
    recovered = _load_workflow_v2_unrecovered(run_dir)
    return recovered if recovered != initial else initial


def _load_workflow_v2_unrecovered(run_dir: Path) -> WorkflowState:
    path = run_dir / "workflow.json"
    try:
        data = _read_json_no_follow(path)
        if isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1:
            raise WorkflowError("explicit migration required")
        return _validate_workflow_v2(data)
    except WorkflowError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise WorkflowError("workflow is invalid") from None


def transition_workflow_v2(run_dir: Path, event: str) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    if not isinstance(event, str):
        raise WorkflowError("workflow transition is invalid")
    with _workflow_lock(run_dir):
        return _transition_workflow_v2_unlocked(run_dir, event)


def _transition_workflow_v2_unlocked(
    run_dir: Path,
    event: str,
    *,
    writer=None,
) -> WorkflowState:
    current = _load_workflow_v2_unrecovered(run_dir)
    next_state = PHASE2_TRANSITIONS.get((current.state, event))
    if next_state is None:
        raise WorkflowError("workflow transition is invalid")
    result = WorkflowState(next_state)
    if writer is None:
        _write_workflow_v2_unlocked(run_dir, result)
    else:
        writer(result)
    return result


def _validate_workflow_v2(data: Any) -> WorkflowState:
    try:
        if not isinstance(data, dict) or set(data) != _DOCUMENT_KEYS or type(data.get("schema_version")) is not int or data["schema_version"] != 2:
            raise ValueError("workflow schema is invalid")
        state, blocked = data.get("state"), data.get("blocked")
        if state not in PHASE2_STATES:
            raise ValueError("workflow state is invalid")
        if blocked is not None:
            _validate_blocked_v2(blocked)
        if (state == "blocked") != (blocked is not None):
            raise ValueError("workflow blocked state is inconsistent")
        return WorkflowState(state, blocked)
    except (TypeError, ValueError):
        raise WorkflowError("workflow is invalid") from None


def _validate_blocked_v2(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _PHASE2_BLOCKED_KEYS:
        raise ValueError("blocked schema is invalid")
    if value["prior_state"] not in set(PHASE2_STATES) - {"blocked", "complete", "rejected"}:
        raise ValueError("blocked prior state is invalid")
    _validate_v2_bounded_string(value["code"], "blocked code")
    for key in ("job_id", "root_failure_key"):
        if value[key] is not None:
            _validate_v2_bounded_string(value[key], f"blocked {key}")
    evidence = value["evidence_path"]
    if evidence is not None:
        _validate_v2_bounded_string(evidence, "blocked evidence path")
        path = Path(evidence)
        if path == Path(".") or path.is_absolute() or ".." in path.parts:
            raise ValueError("blocked evidence path is invalid")
    if value["recoveries"] != []:
        raise ValueError("blocked recoveries are invalid")
    if value["diagnostic"] is not None:
        SafeDiagnostic.from_dict(value["diagnostic"])


def _validate_v2_bounded_string(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or contains_credential_like_text(value)
    ):
        raise ValueError(f"{name} is invalid")


def _read_json_no_follow(path: Path) -> Any:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("JSON document is unsafe")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return json.loads(b"".join(chunks).decode("utf-8"))
    finally:
        os.close(descriptor)


def refresh_workflow(run_dir: Path) -> WorkflowState:
    run_dir = _validated_run_dir(run_dir)
    if _workflow_schema_version(run_dir) == 2:
        return load_workflow_v2(run_dir)
    with _workflow_lock(run_dir):
        return _refresh_workflow_unlocked(run_dir)


def _refresh_workflow_unlocked(run_dir: Path) -> WorkflowState:
    current, _ = _load_workflow_unrecovered(run_dir)
    if _workflow_schema_version(run_dir) == 2:
        return current
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
        _require_phase1_workflow(run_dir)
        return _approve_workflow_stage_unlocked(run_dir, stage, note=note)


def _approve_stage_operation(run_dir: Path, stage: str, *, note: str | None = None):
    run_dir = _validated_run_dir(run_dir)
    with _workflow_lock(run_dir):
        _require_phase1_workflow(run_dir)
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
        _require_phase1_workflow(run_dir)
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
        _require_phase1_workflow(run_dir)
        return _clear_blocked_unlocked(run_dir)


def _require_phase1_workflow(run_dir: Path) -> None:
    if _workflow_schema_version(run_dir) == 2:
        raise WorkflowError("legacy workflow operation is unavailable")


def _workflow_schema_version(run_dir: Path) -> int | None:
    path = run_dir / "workflow.json"
    if not path.exists():
        return None
    try:
        data = _read_json_no_follow(path)
        version = data.get("schema_version") if isinstance(data, dict) else None
        if type(version) is not int or version not in {1, 2}:
            raise ValueError("workflow schema is invalid")
        return version
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise WorkflowError("workflow is invalid") from None


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
        _require_phase1_workflow(run_dir)
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


def _write_workflow_v2_unlocked(run_dir: Path, state: WorkflowState) -> None:
    payload = {"schema_version": 2, "state": state.state, "blocked": state.blocked}
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
def _workflow_lock(run_dir: Path, *, blocking: bool = True):
    with _LOCAL_WORKFLOW_LOCKS_GUARD:
        local_lock = _LOCAL_WORKFLOW_LOCKS.setdefault(run_dir, threading.RLock())
    acquired = local_lock.acquire(blocking=blocking)
    if not acquired:
        yield False
        return
    try:
        with _workflow_file_lock(run_dir, blocking=blocking) as file_acquired:
            yield file_acquired
    finally:
        local_lock.release()


@contextmanager
def _workflow_file_lock(run_dir: Path, *, blocking: bool = True):
    path = run_dir / ".workflow.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WorkflowError("workflow lock path is unsafe")
    existed = path.exists()
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise WorkflowError("workflow lock is unavailable") from None
    file_acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        if not existed:
            _fsync_directory(run_dir)
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB),
            )
        except BlockingIOError:
            yield False
            return
        file_acquired = True
        _validate_lock_identity(descriptor, path=path)
        yield True
    finally:
        pending = None
        if file_acquired:
            try:
                _validate_lock_identity(descriptor, path=path)
            except Exception:
                pending = WorkflowError("workflow lock is unavailable")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        else:
            os.close(descriptor)
        if pending is not None:
            raise pending from None


def _validate_lock_identity(
    descriptor: int,
    *,
    path: Path | None = None,
    directory_fd: int | None = None,
    name: str = ".workflow.lock",
) -> None:
    opened = os.fstat(descriptor)
    current = (
        os.stat(path, follow_symlinks=False)
        if path is not None
        else os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    )
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_nlink != 1
        or current.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise WorkflowError("workflow lock is unavailable")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
