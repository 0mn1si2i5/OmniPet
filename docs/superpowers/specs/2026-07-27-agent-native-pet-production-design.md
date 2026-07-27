# Agent-Native Pet Production Design

## Summary

OmniPet will replace its prompt-first production flow with a governed design and
production harness. The repository-owned skill performs visual reasoning and
user interaction, the engine enforces machine-verifiable workflow contracts,
and each run stores a pet-specific design playbook and audit evidence.

The normal workflow has two owner approval gates:

1. Design Pack approval binds the animation-ready character design, canonical
   image, pose bible, and design contract by SHA-256.
2. Final Package approval binds the completed v2 atlas and review evidence.

Intermediate generation, repair, and QA are agent-owned within the approved
design, budget, and escalation policy. The owner is asked only when a decision
changes identity or approved scope, the next provider call exceeds the
authorized budget, no bounded strategy remains after a repeated root failure,
rights or credentials are missing, or an external, destructive, or remote action
outside the already authorized production contract is required. Ordinary
provider generation within the approved budget and Design Pack is already
authorized and is not an exceptional remote action.

These are exceptional owner interactions, not approval gates. Design Pack and
Final Package approval remain mandatory in every normal run. Clarifying
questions and blocker escalations do not approve artifacts or authorize state
transitions.

## Problem

The current workflow treats an approved base image as sufficient evidence for
all animation states. This fails for references that are attractive still
images but incomplete animation models.

Napoleon exposed the failure clearly:

- the source and canonical show one left-facing three-quarter mounted pose;
- rider, horse, cloak, pointing arm, reins, saddle, and tack form a heavily
  occluded asymmetric compound character;
- row generation repeatedly copied the canonical composition and changed only
  local gestures;
- `running-right` and `running-left` both faced screen-left;
- state rows passed structural QA while failing state semantics, identity
  continuity, and motion coherence;
- multiple prompt revisions and generations reproduced the same root failure.

The missing capability is not another generic prompt clause. OmniPet needs a
mandatory step that decides whether the supplied visual identity is actually
animation-ready, selects an appropriate character abstraction, proves required
pose families, and obtains approval for that complete model before production.

## Goals

- Make the repository the only canonical agent-production authority.
- Separate reusable production judgment from pet-specific conclusions.
- Prevent direct progression from a reference image to nine animation rows.
- Approve an animation model rather than a single attractive base image.
- Adapt prototype evidence to character complexity without burdening simple
  pets with unnecessary pose sheets.
- Keep normal owner interaction to Design Pack and Final Package approval.
- Make structural, semantic, identity, and motion QA explicit and auditable.
- Turn repeated production failures into tested reusable rules.
- Provide versioned machine-readable next actions for reliable resume.

## Non-Goals

- Encoding aesthetic judgment entirely in deterministic Python.
- Making every pet produce a fixed six-pose bible.
- Adding per-row owner approval gates to normal runs.
- Treating model-generated visual verdicts as owner approval.
- Publishing production prompts, references, playbooks, or detailed QA in
  public OmniPets release bundles.
- Preserving implicit compatibility with pre-Phase-2 run state.

## Three-Layer Architecture

### Repository-Owned Production Skill

OmniPet distributes one versioned canonical skill with the repository and
installed package. It is the production entry point for agents and owns:

- intake reasoning and minimal user questions;
- observable, inferred, and unknown fact separation;
- character complexity and animation-readiness classification;
- comparison of viable visual abstractions;
- design-contract and prototype-plan authoring;
- Design Pack presentation and revision dialogue;
- visual semantic, identity, and motion review;
- failure classification, strategy changes, and escalation;
- final-package presentation and owner handoff.

The skill must not duplicate engine schemas or CLI details. It reads versioned
contracts and templates shipped by OmniPet and invokes public CLI operations.
The engine remains authoritative when skill prose and machine contracts differ.

The skill proposes semantic payloads. Public CLI operations validate and persist
them. Only the engine writes workflow state, job manifests, approval records,
artifact hashes, attempt and budget accounting, and checkpoint records. The
run-local playbook is versioned data and has no independent policy authority.

The skill belongs in the OmniPet repository, is included in source and wheel
artifacts, and is versioned with the engine. OmniPet must not depend on a
machine-local Codex or OpenCode skill installation.

### Engine Action And Evidence Contracts

The engine owns all rules that can be validated mechanically:

- workflow state and legal transitions;
- closed JSON schemas and schema versions;
- required artifacts and SHA-256 bindings;
- job dependency graph and prompt/input bindings;
- approval order and invalidation;
- budget and attempt accounting records;
- accepted verdict structure and required reviewer roles;
- checkpoint, package, and release boundaries;
- versioned `status` action contract.

The engine refuses generation when required design artifacts, prototype
evidence, approvals, hashes, or verdicts are missing or stale. Agents do not edit
workflow documents or job manifests directly.

### Run-Local Pet Playbook

Every run stores conclusions specific to that pet. These are facts for the
current production run, not global rules:

```text
.omnipet/runs/<pet-id>/
  workflow.json
  action-state.json
  budget.json
  attempts.jsonl
  design/
    intake.json
    design-contract.json
    design-rationale.md
    prototype-plan.json
    state-storyboard.json
    look-mechanics.json
    design-pack.json
  decoded/
    canonical.png
    prototypes/<pose-id>.png
  qa/
    approvals.json
    design-pack/contact-sheet.png
    design-pack/review.json
    rows/<job-id>/structural.json
    rows/<job-id>/semantic.json
    rows/<job-id>/identity.json
    rows/<job-id>/motion.json
    package/final-visual-review.json
    repair-log.jsonl
```

This layout lists normative governance artifacts; job outputs and deterministic
assembly artifacts continue to use versioned engine paths. Schemas, not this
illustrative tree, are authoritative for the complete file set. All listed
design, session, and QA governance artifacts are excluded from public releases.

The playbook records construction, asymmetry, view coverage, motion freedom,
state grammar, risks, anchor requirements, prohibited strategies, adopted
defaults, owner decisions, and unresolved limitations. Prompt generation reads
the approved playbook; it does not infer these decisions anew for every row.

## Workflow State Machine

The Phase-2 workflow is:

```text
intake
  -> designing
  -> prototyping
  -> awaiting_design_pack_approval
  -> producing_standard_rows
  -> producing_directions
  -> building_package
  -> awaiting_package_approval
  -> complete
```

Any active state may transition to `blocked` with a sanitized diagnostic,
evidence path, root-failure key, and recommended bounded action.

Legal transitions are closed and event-driven:

| Source | Event | Destination | Invalidated evidence | Owner required |
| --- | --- | --- | --- | --- |
| `intake` | intake validated | `designing` | none | no |
| `designing` | contracts validated | `prototyping` | old prototypes and downstream evidence | no |
| `prototyping` | required prototypes pass | `awaiting_design_pack_approval` | none | no |
| `awaiting_design_pack_approval` | approve | `producing_standard_rows` | none | yes |
| `awaiting_design_pack_approval` | revise | `designing` | prototype and downstream evidence | yes |
| `awaiting_design_pack_approval` | reject | `rejected` | retain all audit evidence | yes |
| `producing_standard_rows` | all row verdicts pass | `producing_directions` | none | no |
| `prototyping` or any producing/building state | revise draft or approved design | `designing` | current prototypes or Design Pack approval and downstream evidence | only if an approved Design Pack changes |
| `producing_directions` | direction verdicts pass | `building_package` | none | no |
| `producing_directions` | approved design must change | `designing` | Design Pack approval and downstream evidence | yes |
| `building_package` | package evidence complete | `awaiting_package_approval` | none | no |
| `awaiting_package_approval` | approve | `complete` | none | yes |
| `awaiting_package_approval` | revise package execution | `building_package` | package evidence | yes |
| `awaiting_package_approval` | revise approved design | `designing` | Design Pack approval and downstream evidence | yes |
| `awaiting_package_approval` | reject | `rejected` | retain all audit evidence | yes |
| any nonterminal state | block | `blocked` | none | only when escalation policy requires it |
| `blocked` | resume bounded action | recorded prior state | no implicit invalidation | blocker-dependent |
| `blocked` | revise design | `designing` | draft or approved design and downstream evidence | blocker-dependent |
| approved state | bound artifact changes | earliest owning state | stale approval and downstream evidence | no |

`rejected` and `complete` are terminal. Reopening either creates a new design
revision. A blocked record stores the prior state. Resume cannot infer a default
destination; the selected typed recovery action determines whether work resumes
or returns to design.

### Intake

The engine snapshots user inputs, reference roles, rights declarations, budget,
style requests, and accepted defaults into `design/intake.json`. The skill first
extracts what is directly observable, what is inferred, and what remains unknown.

Low-risk defaults are recorded without asking. The skill asks one question at a
time only when the answer changes identity, character construction, rights,
budget, or the design route.

### Designing

No animation-row visual job exists yet. The skill creates:

- `design-contract.json` for machine-readable decisions;
- `design-rationale.md` for user-readable reasoning;
- `state-storyboard.json` for silhouette and action intent;
- `prototype-plan.json` for required animation evidence.

The engine validates these artifacts before prototype jobs become ready.

### Prototyping

The engine creates only the prototype jobs declared in the validated plan. A
prototype set always contains an animation-ready canonical and may contain
directional, motion-extreme, gesture, or failure anchors.

Prototype requirements are adaptive:

- a symmetric blob may need only a canonical and one motion test;
- a character with a one-sided prop needs explicit left and right anchors;
- a compound asymmetric mounted character may need neutral, screen-left,
  screen-right, airborne, failed/lowered, and gesture anchors;
- a reference with no reliable reverse view must not generate a reverse-facing
  row until the reverse anchor is approved.

The engine validates the plan against a versioned minimum-evidence matrix. The
matrix uses risk flags from the closed design schema:

| Risk flag | Minimum required evidence |
| --- | --- |
| always | animation-ready canonical and one motion-cycle prototype |
| `directional_motion` | screen-left and screen-right anchors |
| `unsafe_mirror` | independently generated anchors for each required side |
| `compound_character` | one prototype proving stable attachment and contact relationships |
| `airborne_state` | grounded anticipation, airborne, and return pose family |
| `state_extreme` | one anchor for every state whose silhouette cannot be derived from an approved family |
| `unsupported_reference_view` | generated and reviewed anchor for that view before dependent jobs |

The skill may require additional evidence. It may reduce a matrix requirement
only through a typed exception that names the risk, rationale, substitute
evidence, and reviewer principal. The engine binds the exception into the Design
Pack. Evaluation fixtures define expected risk flags, minimum anchors, and
forbidden omissions so adaptive behavior has an oracle.

A prototype passes only after structural, view-semantic, identity/construction,
and pose-purpose verdicts pass. Warnings may be presented in the Design Pack but
must be disclosed; a hard failure cannot be approved.

### Design Pack Approval

The Design Pack contains:

- the design contract and rationale;
- animation-ready canonical;
- required pose bible/prototype images;
- prototype contact sheet;
- standard-state silhouette/action storyboard;
- asymmetry and mirroring policy;
- known risks and explicit non-commitments;
- expected provider calls and budget.

`design/design-pack.json` is the authoritative manifest. It contains a design
revision id, schema versions, every directly and transitively bound artifact,
sorted relative paths, SHA-256 values, accepted warnings, budget authorization,
and owner-decision inputs. The owner chooses `approve`, `revise`, or `reject`.
Any later change to a bound artifact or decision invalidates Design Pack
approval and every downstream job and verdict.

Approval means the owner accepts the complete animation model, not merely that
the canonical image looks attractive.

### Standard Rows

Row jobs use only approved design artifacts and the relevant approved pose
anchors. Prompt generation is concise and state-specific; long policy remains in
the skill and engine contracts.

Every row produces four verdicts:

1. Structural: dimensions, frame count, extraction, components, padding, and
   chroma suitability.
2. Semantic: the state and direction are recognizable from pose and silhouette.
3. Identity: face, proportions, handedness, markings, props, attachments, and
   compound-body relationships remain correct.
4. Motion: adjacent frames and loop boundaries are coherent and do not read as
   unrelated redraw morphs.

There is no normal owner approval at this stage. The agent may repair or
regenerate within the approved design and budget. All nine rows must pass before
directions become ready.

### Directions

The Design Pack includes a versioned `design/look-mechanics.json` artifact. It
defines anchored, leading, following, occluding, and trailing parts plus cardinal
pose families. Any screen-left, screen-right, up, or down pose-family anchor
required by the minimum-evidence matrix is generated, reviewed, and bound before
Design Pack approval. After approval, the engine exposes a production cardinal
strip and rows 9 and 10 in dependency order. The production cardinal strip
instantiates the already approved pose families; it is QA evidence, not a new
design prototype. Cardinal semantics, identity, registration, and continuity
must pass before packaging. Any semantic change to look mechanics or its pose
families returns the run to Design Pack revision; post-approval generation may
not invent a new direction model.

There is no normal owner approval at this stage. A required change to the
approved character construction or pose families returns the run to Design Pack
revision instead of silently redefining the pet.

### Package And Final Approval

The engine assembles, cleans, validates, and creates focused QA media. Final
review combines all four QA categories and an independent reviewer role. The
owner approves the complete package. Approval binds package and final-review
artifacts by SHA-256 before publishing or release export is allowed.

## Design Contract

`design-contract.json` is a closed, versioned schema. It contains at least:

- `schema_version` and `skill_contract_version`;
- `pet_id` and stable design revision;
- `character_construction`: bodies, compound relationships, mounted/attached
  relationships, contact points, and stable anchors;
- `reference_view_coverage`: supported view families and unsupported inferred
  views;
- `asymmetries`: handedness, one-sided props, openings, markings, lighting, and
  mirror safety;
- `motion_freedom`: anchored, rigid, articulated, deformable, trailing, and
  occluding parts;
- `state_grammar`: start, key, and return poses plus silhouette evidence for all
  standard states;
- `generation_risks`: occlusion, thin connectors, detail density, multiple
  subjects, reverse-view uncertainty, and extraction risk;
- `prototype_requirements`: required pose ids and their purpose;
- `prohibited_strategies`: pet-specific methods that would violate identity;
- `accepted_defaults` and `owner_decisions`;
- `known_limitations`.

Pet-specific prose such as Napoleon's exact cloak side belongs in this file.
Reusable principles such as requiring reverse-view evidence for an asymmetric
compound character belong in the skill and evaluation suite.

## Versioned Status Action Contract

Human-readable `next_action` remains for CLI users. `status` also returns a
closed machine-readable object. It binds actions to an exact run revision so a
resumed agent cannot execute a stale action:

```json
{
  "action_contract_version": 1,
  "run_revision": "sha256:<workflow-and-evidence-digest>",
  "state": "awaiting_design_pack_approval",
  "actions": [{
      "id": "approve-design-pack:<run-revision>",
      "kind": "approve-design-pack",
      "command": ["omnipet", "approve", "napoleon", "--stage", "design-pack"],
      "required_inputs": [],
      "bound_evidence": [{"path": "design/design-pack.json", "sha256": "<digest>"}],
      "preconditions": [{"kind": "state-is", "value": "awaiting_design_pack_approval"}],
      "owner_required": true,
      "reason_code": "design-pack-ready"
    }, {
      "id": "revise-design-pack:<run-revision>",
      "kind": "revise-design-pack",
      "required_inputs": [{"name": "reason", "type": "non-empty-string"}],
      "bound_evidence": [],
      "preconditions": [{"kind": "state-is", "value": "awaiting_design_pack_approval"}],
      "owner_required": true,
      "reason_code": "owner-revision-option"
  }],
  "budget": {
    "authorized_usd": 5.0,
    "estimated_spent_usd": 0.8,
    "next_call_estimate_usd": 0.04
  }
}
```

Action kinds, typed inputs, evidence bindings, preconditions, and owner flags
drive agent behavior. `command` is explanatory and must not be parsed as the
protocol. Mutating CLI operations accept the action id and run revision and
reject stale values. Blocked states expose bounded recovery alternatives;
terminal states expose an empty `actions` list. Unknown action-contract versions
are fatal for an autonomous agent.

## User Interaction Contract

The skill follows these rules:

1. Inspect inputs before asking questions.
2. Separate observed facts, inferences, and unknowns.
3. Adopt and record low-risk defaults.
4. Ask one question at a time only when it changes identity, construction,
   rights, budget, or design route.
5. When a real trade-off exists, present two or three approaches with visual
   consequences and a recommendation.
6. After enough route-changing questions are resolved, build the complete Design
   Pack without introducing a separate route-approval gate.
7. After Design Pack approval, own intermediate QA and repair.
8. Escalate only for the defined owner-required conditions.

The Design Pack presentation must explain the visual abstraction from source to
sprite, show canonical and pose anchors together, summarize every standard
state's action grammar, state asymmetry/mirroring policy, identify known risks,
and offer explicit `approve`, `revise`, and `reject` outcomes.

## QA Verdict Contract

Every visual verdict uses a closed schema with:

- `schema_version`;
- `job_id`, `category`, `reviewer_role`, `reviewer_principal_id`, and the source
  artifact's `generator_principal_id` and `selection_principal_id`;
- `decision`: `pass`, `warning`, or `fail`;
- structured criteria with visible evidence;
- artifact paths and SHA-256 values;
- engine-normalized failure dimensions for failures;
- a structured next-strategy type and changed input set for failures;
- `reviewed_at`.

Reviewer roles are closed: `deterministic`, `production-agent`,
`independent-visual-reviewer`, and `owner`. Structural scripts may emit only
structural verdicts. Row semantic, identity, and motion review may be authored by
the production agent, but final package visual review must use an independent
principal that did not generate or select package visuals and must inspect the
source QA media directly. Owner approval must use the owner role. The engine
rejects prohibited principal combinations. A structural pass cannot satisfy
semantic, identity, or motion review; aggregate completion requires all four.

Generation and selection operations persist stable principal ids in provenance
records before review begins. A principal may generate and select during normal
production, but the final independent reviewer must differ from both recorded
principals. Missing selection provenance blocks final review.

## Failure Learning And Escalation

Every attempt appends a sanitized record to `qa/repair-log.jsonl`:

- job and attempt id;
- artifact and verdict references;
- canonical failure category and engine-normalized failure dimensions;
- concrete visual or deterministic evidence;
- structured strategy change (`new-anchor`, `simplify-construction`,
  `change-view-family`, `change-extraction`, `change-prompt-contract`, or
  `return-to-design`) and changed inputs;
- result and provider-cost estimate.

The engine derives a root-failure key from canonical category and normalized
dimensions; the skill cannot assign a fresh arbitrary key. Two occurrences of
the same root failure block another unchanged provider call. A third call is
allowed only after the record proves a material structured strategy change. If
that strategy changes the approved design, or no bounded strategy remains, the
engine requires owner interaction. Merely moving the same failure to another
frame counts as recurrence.

Recurrence is counted per design revision across the complete run, not per job.
A strategy change starts a new strategy epoch but does not erase recurrence
history. Two occurrences in the new epoch block that strategy as well; the agent
may choose another bounded strategy or return to design. Owner escalation is
required only when no bounded strategy remains, the approved design must change,
or the next call would exceed budget.

Learning is promoted through three levels:

1. Run-local records preserve pet-specific history.
2. Maintainers manually curate minimal anonymized repository evaluation cases
   for reusable failure patterns.
3. Maintainer-reviewed cross-pet judgment becomes skill policy; mechanically
   enforceable policy becomes engine validation.

Phase 2 records promotion candidates but does not automate promotion. Each
evaluation or policy change is separately reviewed and versioned.

Napoleon's exact handedness stays local. The rule that a single three-quarter
view is insufficient reverse-view evidence for a compound asymmetric character
is reusable and belongs in the skill/evaluation suite.

## Skill Development And Evaluation

The canonical production skill is developed with RED/GREEN/REFACTOR scenarios.

### RED Baseline

Run agents without the new skill against frozen fixtures while holding the
engine version, model, tool permissions, budget, and starting state constant:

- a compound asymmetric mounted character with one three-quarter reference;
- a simple symmetric blob;
- a humanoid with a one-sided held prop;
- a rigid object mascot with a readable front face.

Each fixture contains expected risk flags, minimum and maximum acceptable
prototype sets, required questions, forbidden questions, prohibited strategies,
and expected escalation outcomes. Record whether agents skip animation-readiness analysis, overfit prompts, ask
unnecessary questions, overcomplicate simple pets, mirror unsafe props, or treat
structural QA as visual success.

### GREEN

The minimal skill must cause agents to:

- classify character complexity correctly;
- identify missing view or motion evidence;
- propose an appropriately sized prototype plan;
- ask only route-changing user questions;
- present a complete Design Pack;
- distinguish all four QA categories;
- change strategy after repeated root failure.

### REFACTOR Pressure Tests

Repeat scenarios under combined pressures:

- the user says to skip design and generate immediately;
- most of the budget has already been spent;
- a structurally valid contact sheet looks semantically wrong;
- a previous prompt revision already failed;
- a simple character tempts the agent to create unnecessary prototypes;
- an asymmetric character tempts the agent to mirror a row.

An independent evaluator scores the frozen expectations. Every safety-critical
expectation must pass in every run; noncritical interaction criteria must pass at
least 90% across three runs per fixture. Skill-only tests use a frozen permissive
engine to isolate the skill's effect. Separate integration tests use the strict
Phase-2 engine. The skill passes only when it prevents unsafe skipping without
turning every pet into the maximum-complexity workflow.

## Repository Layout

The repository owns the skill and supporting contracts, for example:

```text
src/omnipet/agent/
  skill/SKILL.md
  references/
    design-contract.md
    qa-verdicts.md
    interaction-and-escalation.md
  schemas/
    action-contract-v1.json
    design-contract-v1.json
    prototype-plan-v1.json
    visual-verdict-v1.json
  evaluations/
    compound-asymmetric-mounted/
    simple-symmetric-blob/
    one-sided-prop/
    rigid-front-face/
```

Package-data tests verify that source distributions, wheels, and editable
installs contain the same skill, schemas, references, and evaluation fixtures.
The repository does not restore an external skill installer.

## Migration And Compatibility

Phase 2 replaces approval stages `base`, `standard-rows`, `directions`, and
`package` with normal owner stages `design-pack` and `package`. Intermediate QA
still exists but is no longer an owner approval stage.

Existing Phase-1 checkpoints and runs do not receive implicit compatibility
logic. They must either:

- continue under the pinned Phase-1 engine; or
- use an explicit migration command that creates a draft design contract,
  marks all inferred fields as unapproved, invalidates downstream evidence, and
  returns the run to `designing`.

The engine must never infer Design Pack approval from an old base approval.
It also never treats an old standard-row, direction, or package approval as a
Phase-2 approval.

Migration first identifies the source workflow schema and engine version. It is
allowed only for a valid, non-publishing Phase-1 run. The operation snapshots an
immutable migration manifest with source artifact hashes, prior attempts, budget
records, repairs, approvals, and provenance; performs an atomic staged rewrite;
invalidates every old approval and all downstream evidence; and returns the run
to `designing`. Failure restores the exact source run. Completed releases remain
immutable and start a new design revision instead of migrating in place. The
supported Phase-1 engine remains installable by its pinned package version; the
Phase-2 engine does not emulate its state machine.

## Testing Strategy

Engine tests cover:

- legal state transitions and blocked transitions;
- closed schema rejection and version handling;
- Design Pack artifact completeness and SHA invalidation;
- adaptive prototype requirements;
- action-contract output for every state;
- four-category verdict completeness and reviewer-role separation;
- root-failure recurrence and provider-call blocking;
- repair invalidation from prototypes, rows, directions, and package;
- checkpoint restore and explicit old-run migration;
- package-data inclusion and public-release exclusion.

Skill tests cover the RED/GREEN/REFACTOR scenarios above and preserve baseline
and post-skill transcripts as evaluation evidence.

An end-to-end validation is not complete until named fixtures
`compound-asymmetric-mounted-v1` and `simple-symmetric-blob-v1` both finish the
two-gate workflow. The complex fixture must produce every matrix-required anchor
and no forbidden mirror derivation. The simple fixture must use no more than its
canonical plus one motion-cycle prototype. Both must follow the expected state
sequence, provide all required verdicts, and pass wheel-install and clean-room
release verification on the supported Python matrix.

## Phase 2 Exit Criteria

Phase 2 is complete only when all of the following exist and pass:

- repository-owned versioned canonical production skill;
- versioned design, prototype, verdict, and action schemas;
- two-gate workflow enforced by the engine;
- run-local design playbook and repair audit;
- four-category QA with independent final review;
- repeated-root and budget escalation enforcement;
- skill RED/GREEN/REFACTOR evaluation evidence;
- package-data and clean-install verification;
- successful replay with one complex asymmetric and one simple pet;
- documentation showing that public release bundles exclude all private design
  and agent-session artifacts.

## Accepted Design Decisions

- Scope is the complete Phase-2 agent-native workflow, not a prompt-only patch.
- Architecture uses repository skill, engine contracts, and run-local playbook.
- Normal owner approvals are Design Pack and Final Package only.
- Prototype plans are adaptive to character complexity.
- The four QA categories and three-level failure-learning model are mandatory
  Phase-2 acceptance criteria.
