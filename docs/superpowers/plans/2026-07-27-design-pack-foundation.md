# Design Pack Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent any standard-row job or provider call before a complete, hash-bound Design Pack has been approved.

**Architecture:** Add packaged closed contracts, an explicit workflow-v2 state machine, a focused Design Pack service, and stale-safe machine actions. New Phase-2 runs start at `intake` without `imagegen-jobs.json`; approved Design Pack evidence atomically creates standard-row jobs and advances to `producing_standard_rows`.

**Tech Stack:** Python 3.12+, standard-library JSON/path/hash/locking APIs, `unittest`, setuptools package data. Follow the repository's handwritten closed-validation style; do not add a schema runtime dependency in this plan.

---

## Scope And Safety

This plan implements:

```text
intake -> designing -> prototyping
       -> awaiting_design_pack_approval -> producing_standard_rows
```

It does not implement Phase-1 data migration, row execution, four-category
production QA, directions, packaging, or blocked recovery. It does disable
implicit Phase-1 approval migration before exposing Phase-2 behavior. It creates
and executes only the prototype jobs declared by the validated plan.

The current worktree contains intentional prompt, adoption, vendoring, CLI, and
test changes. Never revert or replace them. In particular, preserve the rich
prompt builders in `src/omnipet/run.py`; this plan changes when they may be used,
not their content.

Every commit step in this plan is conditional. Do not stage or commit unless the
owner separately authorizes it; without authorization, stop after verification
with changes left in the working tree.

## File Map

Create:

- `src/omnipet/agent/__init__.py`: packaged-resource namespace.
- `src/omnipet/agent/schemas/*.json`: normative closed contracts.
- `src/omnipet/agent/resources.py`: safe packaged resource access.
- `src/omnipet/design_contracts.py`: authoritative Python validators.
- `src/omnipet/design_pack.py`: intake/design/prototype/manifest operations.
- `src/omnipet/actions.py`: run revision and stale-safe actions.
- `tests/design_pack_fixtures.py`: valid payload builders.
- `tests/test_agent_schema_resources.py`: resource and schema inventory.
- `tests/test_design_pack.py`: Design Pack behavior.
- `tests/test_workflow_v2.py`: exact state transitions and no-early-generation.
- `tests/test_action_contract.py`: machine actions and stale rejection.
- `tests/test_design_pack_cli.py`: public vertical slice.
- `tests/test_prototype_jobs.py`: declared prototype job lifecycle.

Modify:

- `pyproject.toml`: package agent resources.
- `src/omnipet/workflow.py`: explicit v2 states/transitions.
- `src/omnipet/approvals.py`: Design Pack SHA approval.
- `src/omnipet/release.py`: Phase-2 initialization/status/approval routing.
- `src/omnipet/run.py`: post-approval row-manifest preparation only.
- `src/omnipet/cli.py`: design submissions and stale-safe approval.
- `src/omnipet/checkpoint.py`: reject implicit Phase-1 approval migration for v2.
- `tests/test_packaging.py`: wheel/sdist/clean-install resources.

### Task 1: Package Foundational Contracts

**Files:**
- Create: `src/omnipet/agent/__init__.py`
- Create: `src/omnipet/agent/resources.py`
- Create: `src/omnipet/agent/schemas/intake-v1.json`
- Create: `src/omnipet/agent/schemas/design-contract-v1.json`
- Create: `src/omnipet/agent/schemas/state-storyboard-v1.json`
- Create: `src/omnipet/agent/schemas/prototype-plan-v1.json`
- Create: `src/omnipet/agent/schemas/look-mechanics-v1.json`
- Create: `src/omnipet/agent/schemas/prototype-evidence-v1.json`
- Create: `src/omnipet/agent/schemas/design-pack-v1.json`
- Create: `src/omnipet/agent/schemas/action-contract-v1.json`
- Create: `src/omnipet/agent/contracts/minimum-evidence-v1.json`
- Create: `src/omnipet/design_contracts.py`
- Test: `tests/test_agent_schema_resources.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing package-resource tests**

Test the exact inventory through `importlib.resources`, JSON readability,
`schema_version == 1`, unique schema IDs, and rejection of unknown resource names:

```python
from omnipet.agent.resources import load_json_resource, resource_inventory

EXPECTED = {
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

class AgentSchemaResourceTests(unittest.TestCase):
    def test_foundational_inventory_is_exact(self):
        self.assertTrue(EXPECTED.issubset(set(resource_inventory())))

    def test_unknown_or_traversing_resource_is_rejected(self):
        for name in ("missing.json", "../pet.yaml", "/tmp/x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_json_resource(name)
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m unittest tests.test_agent_schema_resources -v`

Expected: import failure for `omnipet.agent.resources`.

- [ ] **Step 3: Add minimal packaged resources**

Every file under `schemas/` uses this documentation envelope:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://omnipet.dev/schemas/<name>-v1.json",
  "schema_version": 1,
  "type": "object",
  "additionalProperties": false,
  "required": [],
  "properties": {}
}
```

The authoritative Python validators reject unknown fields, booleans where exact
integers are required, unsafe paths, invalid IDs, duplicate lists, invalid UTC
timestamps, and sensitive text. Schema files document the same closed shapes and
are shipped for agents; Python code remains authoritative.

Implement the complete field shapes from the approved design specification,
including nested closed records for character bodies/relationships/contact
points, supported and unsupported view families, asymmetry/mirror safety, motion
freedom, nine-state grammar, per-state/per-view risk instances, typed prototype
requirements and exceptions, look cardinal families, artifact hashes, typed
action inputs, preconditions, and budget. Fixture builders in Task 3/4 are the
executable examples; unknown nested fields must have one failing test each.

The matrix under `contracts/` is versioned data, not a JSON Schema, and contains:

```json
{
  "schema_version": 1,
  "requirements": {
    "always": ["animation-ready-canonical", "motion-cycle"],
    "directional_motion": ["screen-left-anchor", "screen-right-anchor"],
    "unsafe_mirror": ["screen-left-anchor", "screen-right-anchor"],
    "compound_character": ["stable-attachment"],
    "airborne_state": ["grounded-anticipation", "airborne", "return-pose"],
    "state_extreme": ["state-extreme-anchor"],
    "unsupported_reference_view": ["unsupported-view-anchor"]
  }
}
```

- [ ] **Step 4: Implement safe resource access**

```python
def load_json_resource(name: str) -> dict[str, Any]:
    if not _safe_relative_resource(name):
        raise ValueError("agent resource is invalid")
    value = json.loads(
        files("omnipet.agent").joinpath(name).read_text(encoding="utf-8")
    )
    if not isinstance(value, dict):
        raise ValueError("agent resource is invalid")
    return value
```

- [ ] **Step 5: Package resources**

Add `"agent/schemas/*.json"` and `"agent/contracts/*.json"` to package data.

- [ ] **Step 6: Run GREEN**

Run: `.venv/bin/python -m unittest tests.test_agent_schema_resources -v`

Expected: all tests pass.

- [ ] **Step 7: Commit**

Stage only Task 1 files and inspect `git diff --cached`. Commit
`feat: package Design Pack contracts` only if the owner explicitly authorizes
commits; otherwise leave the verified changes uncommitted.

### Task 2: Add Explicit Workflow V2 Initialization

**Files:**
- Create: `tests/test_workflow_v2.py`
- Modify: `src/omnipet/workflow.py`
- Modify: `src/omnipet/release.py`

- [ ] **Step 1: Write the failing no-early-generation test**

```python
def test_new_phase2_run_starts_at_intake_without_visual_jobs(self):
    state = initialize_design_run(self.run_dir, pet_id="napoleon")
    self.assertEqual(state.state, "intake")
    self.assertFalse((self.run_dir / "imagegen-jobs.json").exists())
    self.assertEqual(
        json.loads((self.run_dir / "workflow.json").read_text()),
        {"schema_version": 2, "state": "intake", "blocked": None},
    )
```

Also patch the generator factory with `side_effect=AssertionError` and prove it
is never called.

- [ ] **Step 2: Run RED**

Run the single test. Expected: `initialize_design_run` is missing.

- [ ] **Step 3: Implement explicit initialization**

Create `workflow.json`, `omnipet-run.json`, `design/`, `decoded/prototypes/`, and
`qa/design-pack/` atomically. `omnipet-run.json` retains the existing reference
snapshot contract and adds the design revision:

```json
{
  "schema_version": 2,
  "pet_id": "napoleon",
  "design_revision": "design-0001",
  "references": [
    {"run_path": "references/reference-01.png", "role": "identity", "sha256": "<digest>"}
  ]
}
```

Do not create prompts or visual jobs and do not invoke vendored run preparation.
Copy and hash reference snapshots through the existing safe-reference logic so
prototype generation never reads mutable project files.

- [ ] **Step 4: Add closed v2 states and transitions**

```python
PHASE2_STATES = (
    "intake", "designing", "prototyping",
    "awaiting_design_pack_approval", "producing_standard_rows",
    "producing_directions", "building_package", "awaiting_package_approval",
    "complete", "blocked", "rejected",
)
PHASE2_TRANSITIONS = {
    ("intake", "intake-validated"): "designing",
    ("designing", "contracts-validated"): "prototyping",
    ("prototyping", "prototypes-passed"): "awaiting_design_pack_approval",
    ("awaiting_design_pack_approval", "design-pack-approved"):
        "producing_standard_rows",
}
```

Schema-1 workflows produce a migration-required error; they are never inferred
as Phase 2.

Disable `migrate_checkpoint_base_approval()` for workflow-v2 restore in the same
RED/GREEN cycle. A Phase-1 checkpoint must report `explicit migration required`;
Plan C later supplies that migration command.

- [ ] **Step 5: Run GREEN and focused regressions**

Run:

```text
.venv/bin/python -m unittest tests.test_workflow_v2 -v
.venv/bin/python -m unittest tests.test_canonical_adoption tests.test_build_current_prompts_rich -v
```

- [ ] **Step 6: Commit**

Commit `feat: initialize agent-native runs at intake`.

### Task 3: Submit And Persist Intake

**Files:**
- Create: `src/omnipet/design_pack.py`
- Create: `tests/design_pack_fixtures.py`
- Create: `tests/test_design_pack.py`
- Modify: `src/omnipet/workflow.py`

- [ ] **Step 1: Write failing tests**

Desired API:

```python
state = submit_intake(run_dir, payload)
self.assertEqual(state.state, "designing")
self.assertEqual(read_json(run_dir / "design/intake.json"), payload)
```

Test identity/revision mismatch, unknown fields, invalid rights, invalid budget,
unsafe reference path, secret-like text, wrong state, and rollback without file
or state mutation.

- [ ] **Step 2: Run RED**

Expected: `omnipet.design_pack` is missing.

- [ ] **Step 3: Implement minimal closed validator and atomic submit**

The payload contains exactly:

```text
schema_version, pet_id, design_revision, references, rights, budget,
style_request, observed_facts, inferred_facts, unknowns,
accepted_defaults, owner_decisions
```

Validate before locking, revalidate identity and state under `_workflow_lock`,
write atomically, then transition with `intake-validated`. On any failure leave
state and bytes unchanged.

- [ ] **Step 4: Run GREEN**

Run `tests.test_design_pack` and `tests.test_workflow_v2`.

- [ ] **Step 5: Commit**

Commit `feat: persist validated design intake`.

### Task 4: Enforce Design And Minimum Prototype Evidence

**Files:**
- Modify: `src/omnipet/design_pack.py`
- Modify: `src/omnipet/design_contracts.py`
- Modify: `tests/design_pack_fixtures.py`
- Modify: `tests/test_design_pack.py`

- [ ] **Step 1: Write failing evidence-matrix tests**

Desired API:

```python
state = submit_design(
    run_dir,
    contract=contract,
    rationale=rationale,
    storyboard=storyboard,
    prototype_plan=plan,
    look_mechanics=look_mechanics,
)
self.assertEqual(state.state, "prototyping")
```

Test missing right anchor, derived right anchor under `unsafe_mirror`, missing
stable attachment, missing airborne family, unsupported view without anchor,
unknown risk, mismatched revisions, typed exception with nonexistent substitute,
and no `imagegen-jobs.json` after success.

- [ ] **Step 2: Run RED**

Expected: `submit_design` is missing.

- [ ] **Step 3: Implement the minimum matrix**

```python
def required_evidence(risk_flags: Iterable[str]) -> frozenset[str]:
    matrix = load_json_resource("contracts/minimum-evidence-v1.json")
    result = set(matrix["requirements"]["always"])
    for flag in risk_flags:
        result.update(matrix["requirements"][flag])
    return frozenset(result)
```

Expand `state_extreme` and `unsupported_reference_view` requirements per affected
state/view; one global placeholder cannot satisfy multiple declared needs.
Require independent left/right methods under `unsafe_mirror`. Typed exceptions
name risk, rationale, substitute pose IDs, and reviewer principal; every
substitute must exist and satisfy the omitted evidence purpose.

- [ ] **Step 4: Persist one atomic design revision**

Publish these files together, then transition:

```text
design/design-contract.json
design/design-rationale.md
design/state-storyboard.json
design/prototype-plan.json
design/look-mechanics.json
```

- [ ] **Step 5: Run GREEN and commit**

Commit `feat: enforce adaptive prototype plans`.

### Task 5: Create And Execute Declared Prototype Jobs

**Files:**
- Create: `tests/test_prototype_jobs.py`
- Modify: `src/omnipet/design_pack.py`
- Modify: `src/omnipet/run.py`
- Modify: `src/omnipet/release.py`
- Modify: `src/omnipet/actions.py`

- [ ] **Step 1: Write failing lifecycle tests**

After design submission, assert the manifest contains exactly the declared
prototype pose IDs with `kind: prototype`, design revision, plan SHA, prompt,
inputs, dependencies, output path, and pending status. Assert no standard or
direction job exists. Test readiness, one visual action per `hatch`, failed-job
blocking, undeclared job rejection, and status actions listing only ready jobs.

- [ ] **Step 2: Run RED**

Run `.venv/bin/python -m unittest tests.test_prototype_jobs -v`.

- [ ] **Step 3: Materialize adaptive prototype jobs**

`submit_design()` atomically writes design artifacts and a prototype-only job
manifest. The animation-ready canonical uses `decoded/canonical.png`; every
other prototype uses `decoded/prototypes/<pose-id>.png`. Each job binds the
design revision, prototype-plan SHA, concise prompt, declared reference roles,
generation method, and exact output path.

Phase-2 grounding reads the retained reference records in `omnipet-run.json`.
After the canonical prototype is selected, promote the same bytes to
`references/canonical-base.png` for compatibility with current row grounding;
`decoded/canonical.png` remains the Design Pack artifact. The Phase-2 path does
not create or depend on `decoded/base.png`.

- [ ] **Step 4: Execute through the engine provider path**

In `prototyping`, `hatch` generates one ready prototype and pauses for evidence.
No standard job is constructible in this state. Plan A records the attempt and
selected source minimally; Plan B later adds budget, recurrence, and append-only
governance around the same provider boundary.

- [ ] **Step 5: Run GREEN and conditionally commit**

Run prototype/design/workflow tests. Commit
`feat: generate declared Design Pack prototypes` only with explicit owner
authorization.

### Task 6: Import Prototype Evidence And Build Design Pack

**Files:**
- Modify: `src/omnipet/design_pack.py`
- Modify: `tests/design_pack_fixtures.py`
- Modify: `tests/test_design_pack.py`

- [ ] **Step 1: Write failing tests**

For each declared pose, import one exact artifact plus four prototype verdicts:
`structural`, `view-semantic`, `identity`, `pose-purpose`. Test missing category,
fail, accepted warning, stale hash, symlink, undeclared pose, wrong revision,
duplicate import, and incomplete prototype set.

- [ ] **Step 2: Run RED**

Expected: `submit_prototype_evidence` is missing.

- [ ] **Step 3: Implement strict prototype import**

The canonical pose uses `decoded/canonical.png`; other poses use
`decoded/prototypes/<pose-id>.png`. Recompute SHA, persist closed
review at `qa/design-pack/prototypes/<pose-id>.json`, and remain in
`prototyping` until all required poses pass.

- [ ] **Step 4: Import Design Pack summary and build manifest**

`submit_design_pack_summary()` imports contact sheet and review, requires the
review budget to equal intake authorization, and creates a deterministic sorted
manifest binding:

```text
all design files
all declared prototype images
all prototype reviews
qa/design-pack/contact-sheet.png
qa/design-pack/review.json
```

The manifest contains schema versions, design revision, accepted warnings,
budget authorization, and owner decisions. Validate the generated manifest,
write it atomically, then transition to `awaiting_design_pack_approval`.

- [ ] **Step 5: Run GREEN and commit**

Commit `feat: build hash-bound Design Packs`.

### Task 7: Approve Design Pack And Materialize Row Jobs

**Files:**
- Modify: `src/omnipet/approvals.py`
- Modify: `src/omnipet/design_pack.py`
- Modify: `src/omnipet/run.py`
- Modify: `src/omnipet/release.py`
- Modify: `tests/test_design_pack.py`
- Modify: `tests/test_workflow_v2.py`

- [ ] **Step 1: Write failing approval tests**

Prove no standard-row job exists before approval; a prototype-only manifest is
expected during prototyping. Approval binds the Design Pack and all transitive
hashes; tampering any bound byte blocks approval; a Phase-1 base approval cannot
satisfy this gate; successful approval atomically replaces the completed
prototype manifest with exactly nine pending row jobs and makes zero provider
calls.

- [ ] **Step 2: Run RED**

Expected: `approve_design_pack` is missing.

- [ ] **Step 3: Implement Design Pack approval**

Under one workflow lock:

1. Require `awaiting_design_pack_approval`.
2. Validate the manifest and every transitive SHA.
3. Build the approval record with owner principal.
4. Rehash all evidence.
5. Build prompts and a standard-row-only manifest from approved data.
6. Atomically publish approval, jobs, prompts, and workflow transition.
7. Restore exact prior bytes on any failure.

The row manifest records `design_revision` and `design_pack_sha256`; it cannot be
used with another revision.

- [ ] **Step 4: Add defense in depth**

`_generate_standard_rows()` independently requires a current Design Pack
approval whose hash matches the manifest. State alone is insufficient.

- [ ] **Step 5: Run GREEN and commit**

Commit `feat: gate row jobs on Design Pack approval`.

### Task 8: Add Stale-Safe Machine Actions

**Files:**
- Create: `src/omnipet/actions.py`
- Create: `tests/test_action_contract.py`
- Modify: `src/omnipet/design_pack.py`
- Modify: `src/omnipet/release.py`

- [ ] **Step 1: Write failing action tests**

Test each state action, exact run revision, sorted bound evidence, typed inputs,
owner flag, standalone `.` command, unknown version rejection, mutation changing
revision, and stale action rejection before any write.

- [ ] **Step 2: Run RED**

Expected: `omnipet.actions` is missing.

- [ ] **Step 3: Implement revision and action contract**

The revision is SHA-256 over canonical JSON containing workflow, run identity,
Design Pack manifest digest, approval digest, and job-manifest digest. Action IDs
are `<kind>:<run_revision>`. Mutations accept both ID and revision and validate
them while holding the workflow lock.

State actions:

```text
intake: submit-intake
designing: submit-design
prototyping: submit-prototype-evidence / submit-design-pack-summary
awaiting_design_pack_approval: approve / revise / reject
producing_standard_rows: hatch or submit visual verdict (implemented in Plan B)
producing_directions: hatch or submit visual verdict (Plan B)
building_package: build-package or submit visual verdict (Plan B)
awaiting_package_approval: approve-package / revise-package / reject-package (Plan B)
blocked: typed recoveries (Plan B)
complete / rejected: no actions
```

Implement `revise-design-pack` and `reject-design-pack`. Package, blocked, and
terminal states have closed action outputs even where Plan B supplies their
mutation handlers. Terminal states always expose an empty action list.

- [ ] **Step 4: Run GREEN and commit**

Commit `feat: expose stale-safe production actions`.

### Task 9: Wire Public CLI Vertical Slice

**Files:**
- Create: `tests/test_design_pack_cli.py`
- Modify: `src/omnipet/cli.py`
- Modify: `src/omnipet/release.py`

- [ ] **Step 1: Write failing parser and E2E tests**

Required commands:

```text
omnipet design intake PET --file intake.json --action-id ... --run-revision ...
omnipet design submit PET --contract ... --rationale ... --storyboard ...
  --prototype-plan ... --look-mechanics ... --action-id ... --run-revision ...
omnipet design prototype PET --file prototype-evidence.json
  --action-id ... --run-revision ...
omnipet design pack PET --contact-sheet ... --review ...
  --action-id ... --run-revision ...
omnipet approve PET --stage design-pack --principal ...
  --action-id ... --run-revision ...
```

Test sanitized failures, no content echo, stale actions, unsupported Phase-1
state, exact state sequence, and no row jobs before approval.

- [ ] **Step 2: Run RED**

Expected: commands are absent.

- [ ] **Step 3: Extract `_build_parser()` and wire APIs**

The CLI reads caller files but only engine APIs persist them. Do not expose
workflow or manifest write commands. Preserve existing dirty `run
refresh-prompts` behavior until its explicit replacement is addressed.

- [ ] **Step 4: Run GREEN and commit**

Commit `feat: add Design Pack CLI workflow`.

### Task 10: Verify Distribution And Vertical Slice

**Files:**
- Modify: `tests/test_packaging.py`
- Modify: affected existing Phase-1 tests only where the new default behavior is intentional.

- [ ] **Step 1: Add failing wheel/sdist/clean-install tests**

Assert exact schema bytes are available through `importlib.resources` from
source, wheel, sdist-derived wheel, and clean install. Assert no external skill
installer and no resources copied into pet projects or public releases.

- [ ] **Step 2: Run RED, implement package patterns, run GREEN**

- [ ] **Step 3: Run focused suite**

```text
.venv/bin/python -m unittest \
  tests.test_agent_schema_resources \
  tests.test_workflow_v2 \
  tests.test_design_pack \
  tests.test_action_contract \
  tests.test_design_pack_cli \
  tests.test_packaging -v
```

- [ ] **Step 4: Run complete suite**

Run `scripts/test-all.sh`. Expected: exit 0.

- [ ] **Step 5: Inspect**

Run `git diff --check`, `git status --short`, and inspect all staged files. Do
not commit unrelated dirty work or secrets.

## Completion Criteria

- New runs cannot create row jobs or call a provider before Design Pack approval.
- Design Pack readiness is backed by adaptive minimum evidence and four prototype review categories.
- Approval binds every transitive artifact and creates rows atomically.
- Stale actions cannot mutate a run.
- Phase-1 approvals never imply Design Pack approval.
- Foundational resources are identical in source, wheel, and sdist-derived wheel.
