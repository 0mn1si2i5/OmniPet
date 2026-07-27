from __future__ import annotations

import re
from typing import Any

from omnipet.agent.resources import load_json_resource
from omnipet.security import contains_credential_like_text


STANDARD_STATES = frozenset({
    "idle", "running-right", "running-left", "waving", "jumping",
    "failed", "waiting", "running", "review",
})
RESERVED_JOB_IDS = STANDARD_STATES | frozenset({
    "base", "look-cardinals", "look-row-9", "look-row-10",
})
POSE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CARDINAL_DIRECTIONS = frozenset({"000", "090", "180", "270"})
RISKS = frozenset({
    "directional_motion", "unsafe_mirror", "compound_character",
    "airborne_state", "state_extreme", "unsupported_reference_view",
})
EVIDENCE_KINDS = frozenset({
    "animation-ready-canonical", "motion-cycle", "screen-left-anchor",
    "screen-right-anchor", "stable-attachment", "grounded-anticipation",
    "airborne", "return-pose",
})
SCOPED_REQUIREMENT = re.compile(
    r"(?:state-extreme-anchor|unsupported-view-anchor):[a-z0-9]+(?:-[a-z0-9]+)*"
)


def _record(value: Any, keys: set[str] | frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("design contract is invalid")
    return value


def _text(value: Any, *, maximum: int = 10_000) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or contains_credential_like_text(value)
    ):
        raise ValueError("design contract is invalid")
    return value


def _texts(value: Any, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or len(value) > 256:
        raise ValueError("design contract is invalid")
    result = [_text(item) for item in value]
    if len(result) != len(set(result)):
        raise ValueError("design contract is invalid")
    return result


def _identity(document: Any, extra: set[str]) -> dict[str, Any]:
    value = _record(document, {"schema_version", "pet_id", "design_revision"} | extra)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("design contract is invalid")
    _text(value["pet_id"])
    _text(value["design_revision"])
    return value


def validate_design_documents(
    contract: Any,
    rationale: Any,
    storyboard: Any,
    plan: Any,
    look: Any,
    *,
    pet_id: str,
    design_revision: str,
    intake: dict[str, Any],
) -> None:
    _validate_contract(contract, intake)
    _validate_storyboard(storyboard)
    _validate_plan(plan)
    _validate_look(look)
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale.encode("utf-8")) > 65_536:
        raise ValueError("design rationale is invalid")
    _text(rationale, maximum=65_536)
    for document in (contract, storyboard, plan, look):
        if document["pet_id"] != pet_id or document["design_revision"] != design_revision:
            raise ValueError("design identity is invalid")
    _validate_consistency(contract, plan, look, intake)


def _validate_contract(value: Any, intake: dict[str, Any]) -> None:
    value = _identity(value, {
        "skill_contract_version", "character_construction", "reference_view_coverage",
        "asymmetries", "motion_freedom", "state_grammar", "generation_risks",
        "prototype_requirements", "prohibited_strategies", "accepted_defaults",
        "owner_decisions", "known_limitations",
    })
    if type(value["skill_contract_version"]) is not int or value["skill_contract_version"] != 1:
        raise ValueError("design contract is invalid")
    construction = _record(value["character_construction"], {"bodies", "relationships", "contact_points", "stable_anchors"})
    if not isinstance(construction["bodies"], list) or not construction["bodies"]:
        raise ValueError("design contract is invalid")
    body_ids = set()
    for body in construction["bodies"]:
        body = _record(body, {"body_id", "kind", "parts"})
        body_id = _text(body["body_id"]); _text(body["kind"])
        body_parts = _texts(body["parts"], nonempty=True)
        if body_id in body_ids:
            raise ValueError("design contract is invalid")
        body_ids.add(body_id)
    if not isinstance(construction["relationships"], list):
        raise ValueError("design contract is invalid")
    for relationship in construction["relationships"]:
        relationship = _record(relationship, {"subject", "relation", "object"})
        if relationship["relation"] not in {"attached", "mounted", "held", "overlapping"}:
            raise ValueError("design contract is invalid")
        if _text(relationship["subject"]) not in body_ids or _text(relationship["object"]) not in body_ids:
            raise ValueError("design contract is invalid")
    if not isinstance(construction["contact_points"], list):
        raise ValueError("design contract is invalid")
    for contact in construction["contact_points"]:
        contact = _record(contact, {"first_part", "second_part", "constraint"})
        _text(contact["first_part"]); _text(contact["second_part"]); _text(contact["constraint"])
    _texts(construction["stable_anchors"], nonempty=True)

    coverage = _record(value["reference_view_coverage"], {"supported", "unsupported"})
    known_references = {item["path"] for item in intake["references"]}
    views = set()
    for supported in coverage["supported"] if isinstance(coverage["supported"], list) else ():
        supported = _record(supported, {"view", "reference_paths"})
        view = _text(supported["view"]); paths = _texts(supported["reference_paths"], nonempty=True)
        if view in views or not set(paths).issubset(known_references):
            raise ValueError("design contract is invalid")
        views.add(view)
    if not views or not isinstance(coverage["unsupported"], list):
        raise ValueError("design contract is invalid")
    unsupported_views = set()
    for unsupported in coverage["unsupported"]:
        unsupported = _record(unsupported, {"view", "reason", "dependent_states"})
        view = _text(unsupported["view"]); _text(unsupported["reason"])
        states = set(_texts(unsupported["dependent_states"], nonempty=True))
        if view in views or view in unsupported_views or not states.issubset(STANDARD_STATES):
            raise ValueError("design contract is invalid")
        unsupported_views.add(view)

    if not isinstance(value["asymmetries"], list):
        raise ValueError("design contract is invalid")
    for item in value["asymmetries"]:
        item = _record(item, {"part", "kind", "mirror_safe"}); _text(item["part"])
        if item["kind"] not in {"handedness", "one-sided-prop", "opening", "marking", "lighting", "other"} or type(item["mirror_safe"]) is not bool:
            raise ValueError("design contract is invalid")
    if not isinstance(value["motion_freedom"], list) or not value["motion_freedom"]:
        raise ValueError("design contract is invalid")
    for item in value["motion_freedom"]:
        item = _record(item, {"part", "freedom"}); _text(item["part"])
        if item["freedom"] not in {"anchored", "rigid", "articulated", "deformable", "trailing", "occluding"}:
            raise ValueError("design contract is invalid")
    _validate_grammar(value["state_grammar"])
    if not isinstance(value["generation_risks"], list):
        raise ValueError("design contract is invalid")
    seen_risks = set()
    unsupported_states = {
        state
        for item in coverage["unsupported"]
        for state in item["dependent_states"]
    }
    for risk in value["generation_risks"]:
        risk = _record(risk, {"risk", "affected_states", "affected_views", "evidence"})
        name = risk["risk"]; states = set(_texts(risk["affected_states"])); affected_views = set(_texts(risk["affected_views"])); _text(risk["evidence"])
        if name not in RISKS or name in seen_risks or not states.issubset(STANDARD_STATES):
            raise ValueError("design contract is invalid")
        if (
            name in {"directional_motion", "unsafe_mirror", "compound_character", "airborne_state", "state_extreme"} and not states
            or name == "unsupported_reference_view" and (
                not affected_views
                or affected_views != unsupported_views
                or states != unsupported_states
            )
        ):
            raise ValueError("design contract is invalid")
        seen_risks.add(name)
    if any(not item["mirror_safe"] for item in value["asymmetries"]) and "unsafe_mirror" not in seen_risks:
        raise ValueError("design contract is invalid")
    if len(construction["bodies"]) > 1 and "compound_character" not in seen_risks:
        raise ValueError("design contract is invalid")
    if unsupported_views and "unsupported_reference_view" not in seen_risks:
        raise ValueError("design contract is invalid")
    _validate_requirements(value["prototype_requirements"])
    for name in ("prohibited_strategies", "known_limitations"):
        _texts(value[name], nonempty=True)
    for name in ("accepted_defaults", "owner_decisions"):
        _texts(value[name])
        if value[name] != intake[name]:
            raise ValueError("design contract is invalid")


def _validate_grammar(value: Any) -> None:
    value = _record(value, STANDARD_STATES)
    for record in value.values():
        record = _record(record, {"start_pose", "key_pose", "return_pose", "silhouette_evidence"})
        for item in record.values(): _text(item)


def _validate_requirements(value: Any) -> None:
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError("design contract is invalid")
    pose_ids = set()
    evidence_kinds = set()
    for item in value:
        item = _record(item, {"pose_id", "purpose", "evidence_kind"})
        pose_id = _text(item["pose_id"]); _text(item["purpose"]); _evidence(item["evidence_kind"])
        if pose_id in pose_ids or item["evidence_kind"] in evidence_kinds:
            raise ValueError("design contract is invalid")
        pose_ids.add(pose_id)
        evidence_kinds.add(item["evidence_kind"])


def _validate_storyboard(value: Any) -> None:
    value = _identity(value, {"states", "shared_silhouette_rules"})
    states = _record(value["states"], STANDARD_STATES)
    for record in states.values():
        record = _record(record, {"silhouette", "action_intent", "key_poses"})
        _text(record["silhouette"]); _text(record["action_intent"]); _texts(record["key_poses"], nonempty=True)
    _texts(value["shared_silhouette_rules"], nonempty=True)


def _validate_plan(value: Any) -> None:
    value = _identity(value, {"prototypes", "exceptions", "estimated_provider_calls"})
    if not isinstance(value["prototypes"], list) or len(value["prototypes"]) < 2:
        raise ValueError("design contract is invalid")
    pose_ids = set(); evidence_kinds = set(); generated = 0
    for item in value["prototypes"]:
        item = _record(item, {"pose_id", "evidence_kind", "covers_requirements", "purpose", "generation_method", "reference_roles"})
        pose_id = _text(item["pose_id"]); _evidence(item["evidence_kind"]); _texts(item["covers_requirements"], nonempty=True); _text(item["purpose"]); _texts(item["reference_roles"])
        for requirement_id in item["covers_requirements"]:
            _evidence(requirement_id)
        if item["evidence_kind"] not in item["covers_requirements"]:
            raise ValueError("design contract is invalid")
        if (
            POSE_ID.fullmatch(pose_id) is None or pose_id in RESERVED_JOB_IDS
            or pose_id in pose_ids or item["evidence_kind"] in evidence_kinds
            or item["generation_method"] != "generate"
        ):
            raise ValueError("design contract is invalid")
        pose_ids.add(pose_id); evidence_kinds.add(item["evidence_kind"]); generated += item["generation_method"] == "generate"
    if type(value["estimated_provider_calls"]) is not int or value["estimated_provider_calls"] != generated:
        raise ValueError("design contract is invalid")
    if not isinstance(value["exceptions"], list):
        raise ValueError("design contract is invalid")
    seen = set()
    for item in value["exceptions"]:
        item = _record(item, {"risk", "omitted_requirement_id", "rationale", "substitute_pose_ids", "reviewer_principal_id"})
        risk = item["risk"]; _evidence(item["omitted_requirement_id"]); _text(item["rationale"]); substitutes = _texts(item["substitute_pose_ids"], nonempty=True); _text(item["reviewer_principal_id"])
        omitted = item["omitted_requirement_id"]
        if risk not in RISKS or omitted in seen or not set(substitutes).issubset(pose_ids) or risk == "unsafe_mirror":
            raise ValueError("design contract is invalid")
        seen.add(omitted)


def _validate_look(value: Any) -> None:
    value = _identity(value, {"parts", "cardinal_families", "occlusion_rules"})
    if not isinstance(value["parts"], list) or not value["parts"]:
        raise ValueError("design contract is invalid")
    known_parts = set()
    for item in value["parts"]:
        item = _record(item, {"part", "mechanic"}); part = _text(item["part"])
        if part in known_parts or item["mechanic"] not in {"anchored", "leading", "following", "occluding", "trailing"}:
            raise ValueError("design contract is invalid")
        known_parts.add(part)
    families = _record(value["cardinal_families"], CARDINAL_DIRECTIONS)
    for item in families.values():
        item = _record(item, {"pose_id", "method", "anchored_parts", "leading_parts", "trailing_parts"}); _text(item["pose_id"])
        if item["method"] not in {"independent", "derived", "approved-family"}:
            raise ValueError("design contract is invalid")
        for name in ("anchored_parts", "leading_parts", "trailing_parts"):
            if not set(_texts(item[name])).issubset(known_parts):
                raise ValueError("design contract is invalid")
    _texts(value["occlusion_rules"], nonempty=True)


def _evidence(value: Any) -> str:
    value = _text(value)
    if value not in EVIDENCE_KINDS and SCOPED_REQUIREMENT.fullmatch(value) is None:
        raise ValueError("design contract is invalid")
    return value


def _validate_consistency(
    contract: dict[str, Any],
    plan: dict[str, Any],
    look: dict[str, Any],
    intake: dict[str, Any],
) -> None:
    requirements = contract["prototype_requirements"]
    prototypes = plan["prototypes"]
    available_roles = {item["role"] for item in intake["references"]}
    if any(
        not item["reference_roles"]
        or not set(item["reference_roles"]).issubset(available_roles)
        for item in prototypes
    ):
        raise ValueError("design contract is invalid")
    expected_records = {(item["pose_id"], item["purpose"], item["evidence_kind"]) for item in requirements}
    actual_records = {(item["pose_id"], item["purpose"], item["evidence_kind"]) for item in prototypes}
    if expected_records != actual_records:
        raise ValueError("design contract is invalid")
    if plan["estimated_provider_calls"] > intake["budget"]["estimated_provider_calls"]:
        raise ValueError("design contract is invalid")
    prototypes_by_id = {item["pose_id"]: item for item in prototypes}
    if any(
        family["pose_id"] not in prototypes_by_id
        for family in look["cardinal_families"].values()
    ):
        raise ValueError("design contract is invalid")
    motion_parts = {item["part"] for item in contract["motion_freedom"]}
    look_parts = {item["part"] for item in look["parts"]}
    if not motion_parts.issubset(look_parts):
        raise ValueError("design contract is invalid")
    by_kind = {item["evidence_kind"]: item for item in prototypes}
    matrix = load_json_resource("contracts/minimum-evidence-v1.json")["requirements"]
    required = set(matrix["always"])
    risks = {item["risk"]: item for item in contract["generation_risks"]}
    for name in risks:
        if name not in {"state_extreme", "unsupported_reference_view"}:
            required.update(matrix[name])
    if "state_extreme" in risks:
        required.update(f"state-extreme-anchor:{state}" for state in risks["state_extreme"]["affected_states"])
    if "unsupported_reference_view" in risks:
        required.update(f"unsupported-view-anchor:{view}" for view in risks["unsupported_reference_view"]["affected_views"])
    allowed_coverage = set(required)
    if any(not set(item["covers_requirements"]).issubset(allowed_coverage) for item in prototypes):
        raise ValueError("design contract is invalid")
    missing = required - set(by_kind)
    exceptions = {item["omitted_requirement_id"]: item for item in plan["exceptions"]}
    for kind in missing:
        exception = exceptions.get(kind)
        substitutes = [] if exception is None else [item for item in prototypes if item["pose_id"] in exception["substitute_pose_ids"]]
        covered = set().union(*(set(item["covers_requirements"]) for item in substitutes)) if substitutes else set()
        if exception is None or exception["risk"] == "unsafe_mirror" or kind not in covered:
            raise ValueError("design contract is invalid")
    if set(exceptions) != missing:
        raise ValueError("design contract is invalid")
    if "unsafe_mirror" in risks:
        for kind in ("screen-left-anchor", "screen-right-anchor"):
            if by_kind[kind]["generation_method"] != "generate":
                raise ValueError("design contract is invalid")
        left = look["cardinal_families"]["270"]
        right = look["cardinal_families"]["090"]
        if left["method"] != "independent" or right["method"] != "independent" or left["pose_id"] != by_kind["screen-left-anchor"]["pose_id"] or right["pose_id"] != by_kind["screen-right-anchor"]["pose_id"]:
            raise ValueError("design contract is invalid")
        for family, requirement in (
            (left, "screen-left-anchor"),
            (right, "screen-right-anchor"),
        ):
            prototype = prototypes_by_id[family["pose_id"]]
            if prototype["generation_method"] != "generate" or requirement not in prototype["covers_requirements"]:
                raise ValueError("design contract is invalid")
    declared_risks = set(risks)
    for exception in plan["exceptions"]:
        omitted = exception["omitted_requirement_id"]
        expected_risk = (
            "state_extreme" if omitted.startswith("state-extreme-anchor:")
            else "unsupported_reference_view" if omitted.startswith("unsupported-view-anchor:")
            else next((name for name, kinds in matrix.items() if name != "always" and omitted in kinds), None)
        )
        if exception["risk"] not in declared_risks or exception["risk"] != expected_risk:
            raise ValueError("design contract is invalid")
        substitutes = [item for item in prototypes if item["pose_id"] in exception["substitute_pose_ids"]]
        if omitted not in set().union(*(set(item["covers_requirements"]) for item in substitutes)):
            raise ValueError("design contract is invalid")
