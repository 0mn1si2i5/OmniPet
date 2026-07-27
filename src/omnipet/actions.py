from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import re
from pathlib import Path
from typing import Any

from omnipet.workflow import PHASE2_STATES, _workflow_lock


_REVISION_PATHS = (
    "workflow.json",
    "omnipet-run.json",
    "qa/approvals-v2.json",
    "imagegen-jobs.json",
)
_REVISION_ROOTS = (
    "design", "prompts/prototypes", "decoded/prototypes", "references",
    "qa/design-pack",
)
_INPUT_TYPES = {"non-empty-string", "path", "boolean", "integer", "number"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"sha256:[0-9a-f]{64}")


class ActionError(RuntimeError):
    """Raised when an action contract or request is invalid or stale."""


def compute_run_revision(run_dir: Path) -> str:
    path = _validated_run_dir(run_dir)
    hashes: dict[str, str] = {}
    try:
        for relative in _REVISION_PATHS:
            target = path / relative
            if not target.exists() and not target.is_symlink():
                continue
            hashes[relative] = _hash_regular_file(target)
        canonical_path = path / "decoded/canonical.png"
        if canonical_path.exists() or canonical_path.is_symlink():
            hashes["decoded/canonical.png"] = _hash_regular_file(canonical_path)
        for root_name in _REVISION_ROOTS:
            root = path / root_name
            if not root.exists() and not root.is_symlink():
                continue
            if root.is_symlink() or not root.is_dir():
                raise ValueError("action evidence is invalid")
            for target in sorted(root.rglob("*")):
                if target.is_dir() and not target.is_symlink():
                    continue
                relative = target.relative_to(path).as_posix()
                hashes[relative] = _hash_regular_file(target)
        canonical = json.dumps(
            hashes, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    except (OSError, TypeError, ValueError):
        raise ActionError("run revision is invalid") from None


def build_action_contract(run_dir: Path, selector: str) -> dict[str, Any]:
    path = _validated_run_dir(run_dir)
    if not isinstance(selector, str) or not selector.strip():
        raise ActionError("action selector is invalid")
    try:
        workflow = _read_json(path / "workflow.json")
        if (
            not isinstance(workflow, dict)
            or set(workflow) != {"schema_version", "state", "blocked"}
            or workflow["schema_version"] != 2
            or workflow["state"] not in PHASE2_STATES
        ):
            raise ValueError("workflow is invalid")
        revision = compute_run_revision(path)
        budget = _budget(path)
        actions = _actions_for_state(
            path, selector.strip(), workflow["state"], workflow["blocked"], revision
        )
        result = {
            "schema_version": 1,
            "action_contract_version": 1,
            "run_revision": revision,
            "state": workflow["state"],
            "actions": actions,
            "budget": budget,
        }
        _validate_contract(result)
        return result
    except ActionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ActionError("action contract is invalid") from None


def validate_action_request(
    run_dir: Path,
    action_id: str,
    run_revision: str,
    expected_kind: str,
) -> dict[str, Any]:
    path = _validated_run_dir(run_dir)
    if not all(isinstance(value, str) and value for value in (
        action_id, run_revision, expected_kind
    )):
        raise ActionError("action request is invalid")
    try:
        with _workflow_lock(path):
            return validate_action_request_unlocked(
                path, action_id, run_revision, expected_kind
            )
    except ActionError:
        raise
    except Exception:
        raise ActionError("action request is invalid") from None


def validate_action_request_unlocked(
    run_dir: Path,
    action_id: str,
    run_revision: str,
    expected_kind: str,
) -> dict[str, Any]:
    if not all(isinstance(value, str) and value for value in (
        action_id, run_revision, expected_kind
    )):
        raise ActionError("action request is invalid")
    contract = build_action_contract(run_dir, ".")
    if contract["run_revision"] != run_revision:
        raise ActionError("action request is stale")
    matching = [item for item in contract["actions"] if item["id"] == action_id]
    if len(matching) != 1 or matching[0]["kind"] != expected_kind:
        raise ActionError("action request does not match")
    return matching[0]


def _actions_for_state(
    run_dir: Path,
    selector: str,
    state: str,
    blocked: Any,
    revision: str,
) -> list[dict[str, Any]]:
    if state in {"complete", "rejected"}:
        return []
    if state == "intake":
        return [_action("submit-intake", revision, selector, inputs=[("file", "path")], reason="intake-required")]
    if state == "designing":
        return [_action(
            "submit-design", revision, selector,
            inputs=[
                ("contract", "path"), ("rationale", "path"), ("storyboard", "path"),
                ("prototype_plan", "path"), ("look_mechanics", "path"),
            ],
            evidence=_existing_evidence(run_dir, ("design/intake.json",)),
            reason="design-contracts-required",
        )]
    if state == "prototyping":
        return _prototype_actions(run_dir, selector, revision)
    if state == "awaiting_design_pack_approval":
        evidence = _existing_evidence(run_dir, ("design/design-pack.json",))
        return [
            _action("approve-design-pack", revision, selector, inputs=[("principal", "non-empty-string")], evidence=evidence, owner=True, reason="design-pack-ready"),
            _action("revise-design-pack", revision, selector, inputs=[("reason", "non-empty-string")], evidence=evidence, owner=True, reason="owner-revision-option"),
            _action("reject-design-pack", revision, selector, inputs=[("reason", "non-empty-string")], evidence=evidence, owner=True, reason="owner-rejection-option"),
        ]
    mappings = {
        "producing_standard_rows": (
            ("hatch-standard-row", "standard-row-ready"),
            ("submit-standard-row-verdict", "standard-row-review-required"),
        ),
        "producing_directions": (
            ("hatch-direction", "direction-ready"),
            ("submit-direction-verdict", "direction-review-required"),
        ),
        "building_package": (
            ("build-package", "package-build-ready"),
            ("submit-package-verdict", "package-review-required"),
        ),
    }
    if state in mappings:
        return [
            _action(kind, revision, selector, inputs=[("file", "path")] if kind.startswith("submit-") else [], reason=reason)
            for kind, reason in mappings[state]
        ]
    if state == "awaiting_package_approval":
        return [
            _action(kind, revision, selector, inputs=[] if kind == "approve-package" else [("reason", "non-empty-string")], owner=True, reason=reason)
            for kind, reason in (
                ("approve-package", "package-ready"),
                ("revise-package", "owner-revision-option"),
                ("reject-package", "owner-rejection-option"),
            )
        ]
    if state == "blocked":
        if not isinstance(blocked, dict) or blocked.get("recoveries") != []:
            raise ValueError("blocked record is invalid")
        return []
    raise ValueError("workflow state is invalid")


def _prototype_actions(run_dir: Path, selector: str, revision: str) -> list[dict[str, Any]]:
    manifest_path = run_dir / "imagegen-jobs.json"
    if not manifest_path.exists():
        return []
    manifest = _read_json(manifest_path)
    jobs = manifest.get("jobs") if isinstance(manifest, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("prototype manifest is invalid")
    statuses = {job.get("id"): job.get("status") for job in jobs if isinstance(job, dict)}
    ready = [
        job for job in jobs
        if isinstance(job, dict) and job.get("status") == "pending"
        and all(statuses.get(item) == "complete" for item in job.get("depends_on", ()))
    ]
    if ready:
        job = ready[0]
        return [_action(
            "generate-prototype", revision, selector,
            inputs=[],
            evidence=_existing_evidence(run_dir, ("imagegen-jobs.json", job.get("prompt_file"))),
            preconditions=[("ready-job-is", job["id"])], reason="prototype-ready",
        )]
    missing = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("status") != "complete":
            continue
        relative = f"qa/design-pack/prototypes/{job['id']}.json"
        if not (run_dir / relative).is_file():
            missing.append(job)
    if missing:
        return [_action(
            "submit-prototype-evidence", revision, selector,
            inputs=[("file", "path"), ("pose_id", "non-empty-string", [job["id"] for job in missing])],
            evidence=_existing_evidence(run_dir, tuple(job.get("output_path") for job in missing)),
            reason="prototype-evidence-required",
        )]
    if jobs and all(isinstance(job, dict) and job.get("status") == "complete" for job in jobs):
        review_paths = tuple(
            f"qa/design-pack/prototypes/{job['id']}.json" for job in jobs
        )
        return [_action(
            "submit-design-pack-summary", revision, selector,
            inputs=[("contact_sheet", "path"), ("review", "path")],
            evidence=_existing_evidence(run_dir, review_paths), reason="design-pack-summary-required",
        )]
    return []


def _action(
    kind: str,
    revision: str,
    selector: str,
    *,
    inputs: list[tuple[Any, ...]],
    evidence: list[dict[str, str]] | None = None,
    preconditions: list[tuple[str, str]] | None = None,
    owner: bool = False,
    reason: str,
) -> dict[str, Any]:
    action_id = f"{kind}:{revision}"
    commands = {
        "submit-intake": ["omnipet", "design", "intake", selector],
        "submit-design": ["omnipet", "design", "submit", selector],
        "generate-prototype": ["omnipet", "hatch", selector],
        "submit-prototype-evidence": ["omnipet", "design", "prototype", selector],
        "submit-design-pack-summary": ["omnipet", "design", "pack", selector],
        "approve-design-pack": ["omnipet", "approve", selector, "--stage", "design-pack"],
        "revise-design-pack": ["omnipet", "design", "revise", selector],
        "reject-design-pack": ["omnipet", "design", "reject", selector],
        "hatch-standard-row": ["omnipet", "hatch", selector],
        "hatch-direction": ["omnipet", "hatch", selector],
    }
    command = commands.get(kind, ["omnipet", "status", selector])
    if kind in {"generate-prototype", "hatch-standard-row", "hatch-direction"}:
        command = [
            *command, "--action-id", action_id, "--run-revision", revision,
        ]
    return {
        "id": action_id,
        "kind": kind,
        "command": command,
        "required_inputs": [
            {"name": item[0], "type": item[1], **({"allowed_values": item[2]} if len(item) == 3 else {})}
            for item in inputs
        ],
        "bound_evidence": sorted(evidence or [], key=lambda item: item["path"]),
        "preconditions": [
            {"kind": "state-is", "value": _state_for_kind(kind)},
            *({"kind": key, "value": value} for key, value in (preconditions or [])),
        ],
        "owner_required": owner,
        "reason_code": reason,
    }


def _state_for_kind(kind: str) -> str:
    if kind == "submit-intake": return "intake"
    if kind == "submit-design": return "designing"
    if kind in {"generate-prototype", "submit-prototype-evidence", "submit-design-pack-summary"}: return "prototyping"
    if kind.endswith("design-pack"): return "awaiting_design_pack_approval"
    if "standard-row" in kind: return "producing_standard_rows"
    if "direction" in kind: return "producing_directions"
    if kind in {"build-package", "submit-package-verdict"}: return "building_package"
    if kind.endswith("package"): return "awaiting_package_approval"
    return "blocked"


def _existing_evidence(run_dir: Path, relatives: tuple[Any, ...]) -> list[dict[str, str]]:
    result = []
    for relative in relatives:
        if not isinstance(relative, str):
            continue
        path = run_dir / relative
        if path.exists() or path.is_symlink():
            result.append({"path": relative, "sha256": _hash_regular_file(path)})
    return sorted(result, key=lambda item: item["path"])


def _budget(run_dir: Path) -> dict[str, int | float]:
    intake_path = run_dir / "design/intake.json"
    authorized: int | float = 0
    if intake_path.exists() or intake_path.is_symlink():
        intake = _read_json(intake_path)
        budget = intake.get("budget") if isinstance(intake, dict) else None
        value = budget.get("authorized_usd") if isinstance(budget, dict) else None
        if type(value) not in {int, float} or value < 0:
            raise ValueError("budget is invalid")
        authorized = value
    return {
        "authorized_usd": authorized,
        "estimated_spent_usd": 0,
        "next_call_estimate_usd": 0,
    }


def _validate_contract(value: dict[str, Any]) -> None:
    keys = {"schema_version", "action_contract_version", "run_revision", "state", "actions", "budget"}
    if (
        not isinstance(value, dict) or set(value) != keys
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or type(value["action_contract_version"]) is not int or value["action_contract_version"] != 1
        or not isinstance(value["run_revision"], str) or _REVISION.fullmatch(value["run_revision"]) is None
        or value["state"] not in PHASE2_STATES
        or not isinstance(value["actions"], list)
    ):
        raise ValueError("action contract is invalid")
    budget = value["budget"]
    if not isinstance(budget, dict) or set(budget) != {
        "authorized_usd", "estimated_spent_usd", "next_call_estimate_usd"
    } or any(
        type(item) not in {int, float} or not math.isfinite(item) or item < 0
        for item in budget.values()
    ):
        raise ValueError("action budget is invalid")
    seen = set()
    for action in value["actions"]:
        if set(action) != {
            "id", "kind", "command", "required_inputs", "bound_evidence",
            "preconditions", "owner_required", "reason_code",
        } or action["id"] in seen or action["id"] != f"{action['kind']}:{value['run_revision']}":
            raise ValueError("action is invalid")
        seen.add(action["id"])
        if (
            not isinstance(action["kind"], str) or not action["kind"]
            or not isinstance(action["command"], list)
            or not action["command"]
            or any(not isinstance(item, str) or not item for item in action["command"])
            or not isinstance(action["required_inputs"], list)
            or not isinstance(action["bound_evidence"], list)
            or not isinstance(action["preconditions"], list)
            or type(action["owner_required"]) is not bool
            or not isinstance(action["reason_code"], str) or not action["reason_code"]
        ):
            raise ValueError("action is invalid")
        for item in action["required_inputs"]:
            if (
                not isinstance(item, dict)
                or set(item) not in ({"name", "type"}, {"name", "type", "allowed_values"})
                or not isinstance(item["name"], str) or not item["name"]
                or item["type"] not in _INPUT_TYPES
                or ("allowed_values" in item and (
                    not isinstance(item["allowed_values"], list) or not item["allowed_values"]
                    or any(not isinstance(entry, str) or not entry for entry in item["allowed_values"])
                    or item["allowed_values"] != list(dict.fromkeys(item["allowed_values"]))
                ))
            ):
                raise ValueError("action input is invalid")
        paths = []
        for item in action["bound_evidence"]:
            if (
                not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not _safe_relative(item["path"])
                or not isinstance(item["sha256"], str) or _SHA256.fullmatch(item["sha256"]) is None
            ):
                raise ValueError("action evidence is invalid")
            paths.append(item["path"])
        if paths != sorted(set(paths)):
            raise ValueError("action evidence is invalid")
        for item in action["preconditions"]:
            if (
                not isinstance(item, dict) or set(item) != {"kind", "value"}
                or not isinstance(item["kind"], str) or not item["kind"]
                or not isinstance(item["value"], str) or not item["value"]
            ):
                raise ValueError("action precondition is invalid")


def _safe_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and path != Path(".") and ".." not in path.parts and path.as_posix() == value


def _validated_run_dir(run_dir: Path) -> Path:
    path = Path(run_dir).absolute()
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ActionError("run directory is invalid")
    return path


def _read_json(path: Path) -> Any:
    return json.loads(_read_regular_bytes(path).decode("utf-8"))


def _read_regular_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("action evidence is invalid")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _hash_regular_file(path: Path) -> str:
    return hashlib.sha256(_read_regular_bytes(path)).hexdigest()
