from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def valid_intake(run_dir: Path) -> dict[str, Any]:
    metadata = read_json(run_dir / "omnipet-run.json")
    return {
        "schema_version": 1,
        "pet_id": metadata["pet_id"],
        "design_revision": metadata["design_revision"],
        "references": [
            {
                "path": reference["run_path"],
                "role": reference["role"],
                "sha256": reference["sha256"],
            }
            for reference in metadata["references"]
        ],
        "rights": {
            "status": "declared",
            "note": "Owner confirms the supplied references may be used.",
        },
        "budget": {
            "authorized_usd": 5.0,
            "estimated_provider_calls": 16,
        },
        "style_request": "Detailed pixel art with a stable silhouette.",
        "observed_facts": ["The subject wears a dark blue coat."],
        "inferred_facts": [],
        "unknowns": [],
        "accepted_defaults": [],
        "owner_decisions": [],
    }


def changed(payload: dict[str, Any], **updates: Any) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result.update(updates)
    return result


def valid_prototype_evidence(run_dir: Path, pose_id: str, *, warning: str | None = None) -> dict[str, Any]:
    metadata = read_json(run_dir / "omnipet-run.json")
    job = next(item for item in read_json(run_dir / "imagegen-jobs.json")["jobs"] if item["id"] == pose_id)
    verdicts = {
        category: {
            "decision": "warning" if warning is not None and category == "identity" else "pass",
            "reviewer_role": "deterministic" if category == "structural" else "production-agent",
            "reviewer_principal_id": "reviewer-1",
            "criteria": [warning if warning is not None and category == "identity" else f"{category} verified"],
        }
        for category in ("structural", "view-semantic", "identity", "pose-purpose")
    }
    return {
        "schema_version": 1,
        "pet_id": metadata["pet_id"],
        "design_revision": metadata["design_revision"],
        "pose_id": pose_id,
        "artifact": {"path": job["output_path"], "sha256": job["metadata"]["source_sha256"]},
        "verdicts": verdicts,
        "accepted_warnings": [warning] if warning is not None else [],
    }


def valid_design_pack_review(run_dir: Path, contact_sheet_path: Path) -> dict[str, Any]:
    intake = read_json(run_dir / "design/intake.json")
    plan = read_json(run_dir / "design/prototype-plan.json")
    reviews = sorted((run_dir / "qa/design-pack/prototypes").glob("*.json"))
    accepted_warnings = []
    for review in reviews:
        for warning in read_json(review)["accepted_warnings"]:
            if warning not in accepted_warnings:
                accepted_warnings.append(warning)
    return {
        "schema_version": 1,
        "decision": "pass",
        "known_risks": ["Only approved prototype evidence is represented."],
        "accepted_warnings": accepted_warnings,
        "expected_provider_calls": plan["estimated_provider_calls"],
        "budget_authorized_usd": intake["budget"]["authorized_usd"],
        "reviewer_principal_id": "design-reviewer-1",
        "reviewed_at": "2026-07-27T12:00:00Z",
        "evidence": {
            "contact_sheet_sha256": hashlib.sha256(contact_sheet_path.read_bytes()).hexdigest(),
            "prototype_reviews": [
                {
                    "path": review.relative_to(run_dir).as_posix(),
                    "sha256": hashlib.sha256(review.read_bytes()).hexdigest(),
                }
                for review in reviews
            ],
        },
    }


STANDARD_STATES = (
    "idle", "running-right", "running-left", "waving", "jumping",
    "failed", "waiting", "running", "review",
)


def valid_design_documents(run_dir: Path) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    intake = read_json(run_dir / "design/intake.json")
    identity = {
        "schema_version": 1,
        "pet_id": intake["pet_id"],
        "design_revision": intake["design_revision"],
    }
    grammar = {
        state: {
            "start_pose": f"{state} start",
            "key_pose": f"{state} key",
            "return_pose": f"{state} return",
            "silhouette_evidence": f"{state} silhouette remains readable",
        }
        for state in STANDARD_STATES
    }
    contract = {
        **identity,
        "skill_contract_version": 1,
        "character_construction": {
            "bodies": [{"body_id": "pet", "kind": "character", "parts": ["body"]}],
            "relationships": [],
            "contact_points": [],
            "stable_anchors": ["feet"],
        },
        "reference_view_coverage": {
            "supported": [{
                "view": "front",
                "reference_paths": [reference["path"] for reference in intake["references"]],
            }],
            "unsupported": [],
        },
        "asymmetries": [],
        "motion_freedom": [{"part": "body", "freedom": "articulated"}],
        "state_grammar": grammar,
        "generation_risks": [],
        "prototype_requirements": [
            {"pose_id": "canonical", "purpose": "identity baseline", "evidence_kind": "animation-ready-canonical"},
            {"pose_id": "cycle", "purpose": "motion continuity", "evidence_kind": "motion-cycle"},
        ],
        "prohibited_strategies": ["Do not mirror asymmetric designs."],
        "accepted_defaults": list(intake["accepted_defaults"]),
        "owner_decisions": list(intake["owner_decisions"]),
        "known_limitations": ["Only the supplied view is directly referenced."],
    }
    storyboard = {
        **identity,
        "states": {
            state: {
                "silhouette": f"Readable {state} silhouette",
                "action_intent": f"Communicate {state}",
                "key_poses": [f"{state}-key"],
            }
            for state in STANDARD_STATES
        },
        "shared_silhouette_rules": ["Keep the head clear of the torso."],
    }
    plan = {
        **identity,
        "prototypes": [
            {"pose_id": "canonical", "evidence_kind": "animation-ready-canonical", "covers_requirements": ["animation-ready-canonical"], "purpose": "identity baseline", "generation_method": "generate", "reference_roles": ["identity"]},
            {"pose_id": "cycle", "evidence_kind": "motion-cycle", "covers_requirements": ["motion-cycle"], "purpose": "motion continuity", "generation_method": "generate", "reference_roles": ["identity"]},
        ],
        "exceptions": [],
        "estimated_provider_calls": 2,
    }
    look = {
        **identity,
        "parts": [{"part": "body", "mechanic": "anchored"}],
        "cardinal_families": {
            direction: {
                "pose_id": "canonical",
                "method": "independent",
                "anchored_parts": ["body"],
                "leading_parts": [],
                "trailing_parts": [],
            }
            for direction in ("000", "090", "180", "270")
        },
        "occlusion_rules": ["Body remains visible."],
    }
    return contract, "The minimum design is intentionally simple and fully specified.", storyboard, plan, look


def napoleon_design_documents(run_dir: Path):
    contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
    contract["character_construction"] = {
        "bodies": [
            {"body_id": "napoleon", "kind": "character", "parts": ["body", "sword-arm", "hat"]},
            {"body_id": "horse", "kind": "mount", "parts": ["body", "legs"]},
        ],
        "relationships": [{"subject": "napoleon", "relation": "mounted", "object": "horse"}],
        "contact_points": [{"first_part": "napoleon", "second_part": "horse", "constraint": "seat remains attached"}],
        "stable_anchors": ["seat", "horse-feet"],
    }
    contract["asymmetries"] = [{"part": "sword-arm", "kind": "handedness", "mirror_safe": False}]
    contract["motion_freedom"] = [
        {"part": "horse", "freedom": "articulated"},
        {"part": "sword-arm", "freedom": "anchored"},
    ]
    look["parts"] = [
        {"part": "horse", "mechanic": "anchored"},
        {"part": "sword-arm", "mechanic": "leading"},
    ]
    for family in look["cardinal_families"].values():
        family.update(anchored_parts=["horse"], leading_parts=["sword-arm"])
    contract["reference_view_coverage"]["unsupported"] = [
        {"view": "rear", "reason": "No rear reference", "dependent_states": ["running-left", "review"]},
        {"view": "overhead", "reason": "No overhead reference", "dependent_states": ["jumping"]},
    ]
    contract["generation_risks"] = [
        {"risk": "directional_motion", "affected_states": ["running-left", "running-right"], "affected_views": [], "evidence": "Both travel directions are required."},
        {"risk": "unsafe_mirror", "affected_states": ["running-left", "running-right"], "affected_views": [], "evidence": "Sword handedness cannot be mirrored."},
        {"risk": "compound_character", "affected_states": ["running", "jumping"], "affected_views": [], "evidence": "Rider and horse must remain attached."},
        {"risk": "airborne_state", "affected_states": ["jumping"], "affected_views": [], "evidence": "Jump needs a grounded arc."},
        {"risk": "state_extreme", "affected_states": ["failed", "review"], "affected_views": [], "evidence": "Each extreme needs its own silhouette proof."},
        {"risk": "unsupported_reference_view", "affected_states": ["running-left", "review", "jumping"], "affected_views": ["rear", "overhead"], "evidence": "Each missing view needs a distinct anchor."},
    ]
    evidence = [
        ("left", "screen-left-anchor", "running-left direction"),
        ("right", "screen-right-anchor", "running-right direction"),
        ("attachment", "stable-attachment", "rider mount attachment"),
        ("anticipation", "grounded-anticipation", "jump anticipation"),
        ("airborne", "airborne", "jump airborne phase"),
        ("return", "return-pose", "jump return"),
        ("extreme-failed", "state-extreme-anchor:failed", "failed state extreme"),
        ("extreme-review", "state-extreme-anchor:review", "review state extreme"),
        ("view-rear", "unsupported-view-anchor:rear", "rear view anchor"),
        ("view-overhead", "unsupported-view-anchor:overhead", "overhead view anchor"),
    ]
    for pose_id, kind, purpose in evidence:
        requirement = {"pose_id": pose_id, "purpose": purpose, "evidence_kind": kind}
        contract["prototype_requirements"].append(requirement)
        plan["prototypes"].append({
            **requirement,
            "covers_requirements": [kind],
            "generation_method": "generate",
            "reference_roles": ["identity"],
        })
    plan["estimated_provider_calls"] = len(plan["prototypes"])
    look["cardinal_families"]["090"].update(pose_id="right", method="independent")
    look["cardinal_families"]["270"].update(pose_id="left", method="independent")
    return contract, rationale + " Napoleon and horse are treated as a compound articulated character.", storyboard, plan, look
