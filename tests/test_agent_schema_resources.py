import unittest
from unittest.mock import patch

from omnipet.agent.resources import load_json_resource, resource_inventory


EXPECTED_RESOURCES = {
    "schemas/intake-v1.json",
    "schemas/design-contract-v1.json",
    "schemas/state-storyboard-v1.json",
    "schemas/prototype-plan-v1.json",
    "schemas/look-mechanics-v1.json",
    "schemas/prototype-evidence-v1.json",
    "schemas/design-pack-v1.json",
    "schemas/action-contract-v1.json",
    "contracts/minimum-evidence-v1.json",
}
SCHEMA_RESOURCES = {
    name for name in EXPECTED_RESOURCES if name.startswith("schemas/")
}
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
MATRIX_REQUIREMENTS = {
    "always": ["animation-ready-canonical", "motion-cycle"],
    "directional_motion": ["screen-left-anchor", "screen-right-anchor"],
    "unsafe_mirror": ["screen-left-anchor", "screen-right-anchor"],
    "compound_character": ["stable-attachment"],
    "airborne_state": ["grounded-anticipation", "airborne", "return-pose"],
    "state_extreme": ["state-extreme-anchor"],
    "unsupported_reference_view": ["unsupported-view-anchor"],
}
STANDARD_STATES = {
    "idle",
    "running-right",
    "running-left",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
}
CARDINAL_DIRECTIONS = {"000", "090", "180", "270"}
PROTOTYPE_VERDICT_CATEGORIES = {
    "structural",
    "view-semantic",
    "identity",
    "pose-purpose",
}


class _FakeResourceRoot:
    def __init__(self, content):
        self.content = content

    def joinpath(self, *parts):
        return self

    def read_text(self, encoding):
        if isinstance(self.content, Exception):
            raise self.content
        return self.content


def _assert_object_nodes_are_closed(testcase, node, path="$"):
    if isinstance(node, dict):
        if node.get("type") == "object":
            testcase.assertIs(
                node.get("additionalProperties"),
                False,
                f"object schema is not closed at {path}",
            )
        for key in ("properties", "$defs"):
            for name, child in node.get(key, {}).items():
                _assert_object_nodes_are_closed(
                    testcase, child, f"{path}/{key}/{name}"
                )
        if "items" in node:
            _assert_object_nodes_are_closed(testcase, node["items"], f"{path}/items")
        for key in ("oneOf", "anyOf", "allOf"):
            for index, child in enumerate(node.get(key, [])):
                _assert_object_nodes_are_closed(
                    testcase, child, f"{path}/{key}/{index}"
                )


def _assert_closed_record(testcase, schema, required_fields):
    testcase.assertEqual(schema["type"], "object")
    testcase.assertIs(schema["additionalProperties"], False)
    testcase.assertTrue(set(required_fields).issubset(schema["required"]))
    testcase.assertTrue(set(required_fields).issubset(schema["properties"]))


def _assert_exact_keyed_records(testcase, schema, keys, record_fields):
    _assert_closed_record(testcase, schema, keys)
    testcase.assertEqual(set(schema["required"]), set(keys))
    testcase.assertEqual(set(schema["properties"]), set(keys))
    for key in keys:
        with testcase.subTest(key=key):
            _assert_closed_record(testcase, schema["properties"][key], record_fields)


class AgentSchemaResourceTests(unittest.TestCase):
    def test_foundational_inventory_contains_required_resources(self):
        self.assertTrue(EXPECTED_RESOURCES.issubset(set(resource_inventory())))

    def test_all_foundational_resources_are_versioned_json_objects(self):
        for name in EXPECTED_RESOURCES:
            with self.subTest(name=name):
                resource = load_json_resource(name)
                self.assertIsInstance(resource, dict)
                self.assertEqual(resource["schema_version"], 1)

    def test_schema_ids_are_unique(self):
        schema_ids = [
            load_json_resource(name)["$id"] for name in SCHEMA_RESOURCES
        ]

        self.assertEqual(len(schema_ids), len(set(schema_ids)))

    def test_schemas_use_closed_draft_2020_12_documentation_envelope(self):
        for name in SCHEMA_RESOURCES:
            with self.subTest(name=name):
                schema = load_json_resource(name)
                self.assertEqual(schema["$schema"], DRAFT_2020_12)
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                _assert_object_nodes_are_closed(self, schema)

    def test_minimum_evidence_matrix_is_complete(self):
        matrix = load_json_resource("contracts/minimum-evidence-v1.json")

        self.assertEqual(matrix["requirements"], MATRIX_REQUIREMENTS)

    def test_intake_schema_has_closed_reference_rights_and_budget_records(self):
        schema = load_json_resource("schemas/intake-v1.json")
        _assert_closed_record(
            self,
            schema,
            ("schema_version", "pet_id", "design_revision", "references", "rights", "budget"),
        )
        _assert_closed_record(
            self, schema["properties"]["references"]["items"], ("path", "role", "sha256")
        )
        _assert_closed_record(
            self, schema["properties"]["rights"], ("status", "note")
        )
        _assert_closed_record(
            self,
            schema["properties"]["budget"],
            ("authorized_usd", "estimated_provider_calls"),
        )

    def test_intake_schema_matches_runtime_identity_and_text_constraints(self):
        properties = load_json_resource("schemas/intake-v1.json")["properties"]

        self.assertEqual(
            properties["pet_id"]["pattern"],
            "^[a-z0-9]+(?:-[a-z0-9]+)*$",
        )
        self.assertEqual(
            properties["design_revision"]["pattern"],
            "^design-[0-9]{4}$",
        )
        self.assertEqual(properties["style_request"]["minLength"], 1)
        self.assertEqual(properties["rights"]["properties"]["note"]["minLength"], 1)
        self.assertEqual(properties["observed_facts"]["minItems"], 1)
        for field in ("inferred_facts", "unknowns", "accepted_defaults", "owner_decisions"):
            with self.subTest(field=field):
                self.assertIs(properties[field]["uniqueItems"], True)
                self.assertEqual(properties[field]["minItems"], 0)
                self.assertEqual(properties[field]["items"]["minLength"], 1)

    def test_intake_schema_documents_all_expressible_runtime_constraints(self):
        schema = load_json_resource("schemas/intake-v1.json")
        properties = schema["properties"]
        references = properties["references"]
        reference = references["items"]["properties"]

        self.assertEqual(references["maxItems"], 64)
        self.assertIs(references["uniqueItems"], True)
        self.assertEqual(reference["path"]["maxLength"], 256)
        self.assertIn("\\.\\.", reference["path"]["pattern"])
        self.assertEqual(reference["role"]["maxLength"], 10_000)
        self.assertIn("\\x00", reference["role"]["pattern"])
        self.assertEqual(reference["sha256"]["pattern"], "^[0-9a-f]{64}$")
        self.assertEqual(properties["budget"]["properties"]["authorized_usd"]["exclusiveMinimum"], 0)
        self.assertEqual(properties["budget"]["properties"]["estimated_provider_calls"]["minimum"], 1)
        for field in (
            "observed_facts", "inferred_facts", "unknowns",
            "accepted_defaults", "owner_decisions",
        ):
            self.assertEqual(properties[field]["maxItems"], 256)
            self.assertEqual(properties[field]["items"]["maxLength"], 10_000)
            self.assertIn("\\x00", properties[field]["items"]["pattern"])
        self.assertEqual(properties["style_request"]["maxLength"], 10_000)
        self.assertEqual(properties["rights"]["properties"]["note"]["maxLength"], 10_000)
        self.assertEqual(
            schema["x-omnipet-runtime-validation"],
            [
                "fact-values-unique-across-categories",
                "credential-like-sensitive-text-rejected",
                "reference-path-normalized-and-safe",
                "reference-records-match-run-metadata-and-snapshot-bytes",
                "exact-python-types-reject-boolean-numbers",
                "reference-path-uniqueness",
            ],
        )

    def test_design_contract_schema_has_required_closed_design_records(self):
        schema = load_json_resource("schemas/design-contract-v1.json")
        properties = schema["properties"]
        _assert_closed_record(
            self,
            properties["character_construction"],
            ("bodies", "relationships", "contact_points", "stable_anchors"),
        )
        _assert_closed_record(
            self,
            properties["reference_view_coverage"],
            ("supported", "unsupported"),
        )
        for name, fields in {
            "asymmetries": ("part", "kind", "mirror_safe"),
            "motion_freedom": ("part", "freedom"),
            "generation_risks": ("risk", "affected_states", "affected_views", "evidence"),
            "prototype_requirements": ("pose_id", "purpose", "evidence_kind"),
        }.items():
            with self.subTest(record=name):
                _assert_closed_record(self, properties[name]["items"], fields)

    def test_design_contract_state_grammar_has_unique_standard_state_keys(self):
        state_grammar = load_json_resource("schemas/design-contract-v1.json")["properties"]["state_grammar"]
        _assert_exact_keyed_records(
            self,
            state_grammar,
            STANDARD_STATES,
            ("start_pose", "key_pose", "return_pose", "silhouette_evidence"),
        )

    def test_storyboard_schema_has_unique_standard_state_keys(self):
        states = load_json_resource("schemas/state-storyboard-v1.json")["properties"]["states"]
        _assert_exact_keyed_records(
            self,
            states,
            STANDARD_STATES,
            ("silhouette", "action_intent", "key_poses"),
        )

    def test_prototype_plan_schema_has_typed_prototypes_and_exceptions(self):
        properties = load_json_resource("schemas/prototype-plan-v1.json")["properties"]
        _assert_closed_record(
            self,
            properties["prototypes"]["items"],
            ("pose_id", "evidence_kind", "covers_requirements", "purpose", "generation_method", "reference_roles"),
        )
        _assert_closed_record(
            self,
            properties["exceptions"]["items"],
            ("risk", "omitted_requirement_id", "rationale", "substitute_pose_ids", "reviewer_principal_id"),
        )

    def test_design_requirement_and_risk_schema_domains_are_closed(self):
        contract = load_json_resource("schemas/design-contract-v1.json")["properties"]
        plan = load_json_resource("schemas/prototype-plan-v1.json")["properties"]
        contract_kind = contract["prototype_requirements"]["items"]["properties"]["evidence_kind"]
        plan_kind = plan["prototypes"]["items"]["properties"]["evidence_kind"]
        coverage = plan["prototypes"]["items"]["properties"]["covers_requirements"]["items"]
        omitted = plan["exceptions"]["items"]["properties"]["omitted_requirement_id"]
        for domain in (contract_kind, plan_kind, coverage, omitted):
            self.assertIn("oneOf", domain)
            self.assertEqual(len(domain["oneOf"]), 2)
            self.assertIn("enum", domain["oneOf"][0])
            self.assertIn("pattern", domain["oneOf"][1])
        risk_enum = contract["generation_risks"]["items"]["properties"]["risk"]["enum"]
        self.assertEqual(plan["exceptions"]["items"]["properties"]["risk"]["enum"], risk_enum)

    def test_design_schema_documents_runtime_collection_and_text_bounds(self):
        contract = load_json_resource("schemas/design-contract-v1.json")["properties"]
        plan = load_json_resource("schemas/prototype-plan-v1.json")["properties"]
        prototype = plan["prototypes"]["items"]["properties"]
        exception = plan["exceptions"]["items"]["properties"]

        self.assertIs(contract["generation_risks"]["uniqueItems"], True)
        self.assertIs(contract["prototype_requirements"]["uniqueItems"], True)
        self.assertIs(plan["prototypes"]["uniqueItems"], True)
        self.assertIs(plan["exceptions"]["uniqueItems"], True)
        self.assertEqual(prototype["covers_requirements"]["minItems"], 1)
        self.assertIs(prototype["covers_requirements"]["uniqueItems"], True)
        self.assertEqual(exception["substitute_pose_ids"]["minItems"], 1)
        self.assertIs(exception["substitute_pose_ids"]["uniqueItems"], True)
        for field in (prototype["pose_id"], prototype["purpose"], exception["rationale"], exception["reviewer_principal_id"]):
            self.assertEqual(field["minLength"], 1)
            self.assertEqual(field["maxLength"], 10_000)
            self.assertIn("\\x00", field["pattern"])
        for domain in (
            prototype["evidence_kind"],
            prototype["covers_requirements"]["items"],
            exception["omitted_requirement_id"],
        ):
            self.assertEqual(domain["oneOf"][1]["pattern"], "^(?:state-extreme-anchor|unsupported-view-anchor):[a-z0-9]+(?:-[a-z0-9]+)*$")

    def test_look_mechanics_schema_has_unique_cardinal_direction_keys(self):
        properties = load_json_resource("schemas/look-mechanics-v1.json")["properties"]
        _assert_closed_record(
            self, properties["parts"]["items"], ("part", "mechanic")
        )
        _assert_exact_keyed_records(
            self,
            properties["cardinal_families"],
            CARDINAL_DIRECTIONS,
            ("pose_id", "method", "anchored_parts", "leading_parts", "trailing_parts"),
        )

    def test_prototype_evidence_schema_has_unique_verdict_category_keys(self):
        properties = load_json_resource("schemas/prototype-evidence-v1.json")["properties"]
        _assert_closed_record(self, properties["artifact"], ("path", "sha256"))
        _assert_exact_keyed_records(
            self,
            properties["verdicts"],
            PROTOTYPE_VERDICT_CATEGORIES,
            ("decision", "reviewer_role", "reviewer_principal_id", "criteria"),
        )
        verdicts = properties["verdicts"]["properties"]
        self.assertEqual(
            verdicts["structural"]["properties"]["reviewer_role"]["enum"],
            ["deterministic"],
        )
        for category in ("view-semantic", "identity", "pose-purpose"):
            self.assertEqual(
                verdicts[category]["properties"]["reviewer_role"]["enum"],
                ["production-agent", "independent-visual-reviewer"],
            )

    def test_design_pack_schema_has_typed_artifacts_hashes_and_budget(self):
        properties = load_json_resource("schemas/design-pack-v1.json")["properties"]
        _assert_closed_record(
            self, properties["artifacts"]["items"], ("path", "sha256", "kind")
        )
        _assert_closed_record(
            self, properties["hashes"], ("prototype_plan_sha256", "contact_sheet_sha256")
        )
        _assert_closed_record(
            self, properties["budget_authorization"], ("authorized_usd", "estimated_provider_calls")
        )

    def test_action_contract_schema_has_typed_inputs_preconditions_and_budget(self):
        properties = load_json_resource("schemas/action-contract-v1.json")["properties"]
        action = properties["actions"]["items"]
        _assert_closed_record(
            self,
            action,
            ("id", "kind", "required_inputs", "preconditions", "owner_required", "reason_code"),
        )
        _assert_closed_record(
            self, action["properties"]["required_inputs"]["items"], ("name", "type")
        )
        _assert_closed_record(
            self, action["properties"]["preconditions"]["items"], ("kind", "value")
        )
        _assert_closed_record(
            self,
            properties["budget"],
            ("authorized_usd", "estimated_spent_usd", "next_call_estimate_usd"),
        )

    def test_invalid_resource_names_are_rejected(self):
        for name in (
            "missing.json",
            "../pet.yaml",
            "schemas/../../pet.yaml",
            "/tmp/x",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_json_resource(name)

    def test_invalid_resource_content_is_sanitized_without_exception_cause(self):
        invalid_utf8 = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        for content in ("{", invalid_utf8, "[]"):
            with self.subTest(content=repr(content)):
                with (
                    patch(
                        "omnipet.agent.resources.resource_inventory",
                        return_value=["schemas/test.json"],
                    ),
                    patch(
                        "omnipet.agent.resources.files",
                        return_value=_FakeResourceRoot(content),
                    ),
                    self.assertRaises(ValueError) as raised,
                ):
                    load_json_resource("schemas/test.json")
                self.assertEqual(str(raised.exception), "agent resource is invalid")
                self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
