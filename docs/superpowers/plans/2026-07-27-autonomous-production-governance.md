# Autonomous Production Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent autonomously generate, review, repair, and advance standard and direction jobs within the approved Design Pack and budget, while the engine enforces evidence, recurrence, provenance, and reviewer independence.

**Architecture:** Add four-category visual verdicts, append-only attempt/provenance logs, normalized root-failure policy, atomic budget reservations, typed blocked recovery, and evidence-owner invalidation. Standard rows and directions advance through engine-accepted QA without owner approval; only Design Pack and Final Package remain owner gates.

**Tech Stack:** Python 3.12+, standard library, existing workflow locks/atomic transactions, `unittest`.

---

## Preconditions

Plan A is complete: workflow v2, Design Pack approval, stale-safe action contract,
standard jobs bound to an approved design revision, and packaged contracts.

Every commit instruction below is conditional on separate owner authorization.
Otherwise leave verified changes uncommitted.

## File Map

Create:

- `src/omnipet/visual_verdicts.py`
- `src/omnipet/production_audit.py`
- `src/omnipet/production_policy.py`
- `src/omnipet/agent/schemas/visual-verdict-v1.json`
- `src/omnipet/agent/schemas/attempt-record-v1.json`
- `src/omnipet/agent/schemas/repair-record-v1.json`
- `tests/test_visual_verdicts.py`
- `tests/test_production_audit.py`
- `tests/test_production_policy.py`
- `tests/test_phase2_production_workflow.py`

Modify `release.py`, `workflow.py`, `actions.py`, `approvals.py`, `repair.py`,
`package.py`, `cli.py`, and focused existing tests.

### Task 1: Four-Category Visual Verdicts

- [ ] Write failing tests for exact categories `structural`, `semantic`,
  `identity`, `motion`; closed roles/principals; evidence SHA binding; strict
  decision/failure/strategy shapes; and aggregate requiring four passes.
- [ ] Run `.venv/bin/python -m unittest tests.test_visual_verdicts -v`; verify
  import failure.
- [ ] Add `visual-verdict-v1.json` and Python closed validators.
- [ ] Persist standard-row verdicts at `qa/rows/<job>/<category>.json` and use
  these normative roots for other job kinds:

```text
qa/directions/jobs/<job>/<category>.json
qa/package/<category>.json
```

`load_job_verdicts()` maps each job kind to exactly one root. Aggregate direction
and package manifests bind these files but never replace the four categories.
- [ ] Enforce deterministic-only structural review, prohibit owner as production
  reviewer, and bind generator/selector provenance.
- [ ] Run GREEN and commit `feat: add four-category visual verdicts`.

Desired API:

```python
record_visual_verdict(run_dir, payload) -> VisualVerdict
load_job_verdicts(run_dir, job_id) -> dict[str, VisualVerdict]
visual_job_passes(run_dir, job_id) -> bool
```

### Task 2: Append-Only Attempts And Provenance

- [ ] Write failing tests for canonical newline JSON, mode `0600`, no rewrite,
  duplicate event rejection, valid event order, selection only from completed
  attempt, superseding selection by append, and malformed/symlink rejection.
- [ ] Implement `attempts.jsonl`, generation principal, selection principal, and
  `qa/repair-log.jsonl` replay using one safe append primitive.
- [ ] Require selected provenance before visual verdict import.
- [ ] Run audit and verdict tests; commit
  `feat: persist append-only production provenance`.

Desired API:

```python
append_attempt_event(run_dir, payload)
record_selection(run_dir, job_id, attempt_id, principal_id)
load_job_provenance(run_dir, job_id) -> JobProvenance
append_repair_record(run_dir, payload)
```

### Task 3: Normalize Root Failures And Strategy Epochs

- [ ] Write failing tests proving key equality across jobs/frames/order, first
  two occurrences allowed, third unchanged call blocked, material change opens a
  new epoch, history survives epochs, and revision isolation.
- [ ] Implement engine-derived key:

```python
f"{canonical_category}:{','.join(sorted(set(dimensions)))}"
```

- [ ] Accept only closed failure categories/dimensions and strategy types:
  `new-anchor`, `simplify-construction`, `change-view-family`,
  `change-extraction`, `change-prompt-contract`, `return-to-design`.
- [ ] Append normalized repair record from failed verdict; never accept a
  caller-supplied key or epoch.
- [ ] Run GREEN and commit `feat: enforce root-failure strategy epochs`.

### Task 4: Atomic Budget Gate Around Every Provider Call

- [ ] Write failing tests for exact Decimal accounting, unresolved reservations,
  restart recovery, concurrency, exact-limit acceptance, over-limit block,
  double-finalize rejection, and recurrence/budget checked under one lock.
- [ ] Implement:

```python
authorize_provider_call(...) -> ProviderReservation
finalize_provider_call(..., outcome, artifact, estimated_cost)
```

- [ ] Route prototype, row, cardinal, and direction provider calls through one
  `_run_governed_generation()` helper in `release.py`.
- [ ] Prove a blocked call invokes the generator zero times.
- [ ] Run production policy/release/OpenAI tests and commit
  `feat: gate provider calls by budget and recurrence`.

### Task 5: Autonomous Standard And Direction Progression

- [ ] Write failing workflow tests: structural-only does not advance; any
  warning/fail blocks; all four passes for every row advance to directions; the
  direction dependency sequence remains cardinal -> row 9 -> row 10; no
  intermediate approval record or action exists.
- [ ] Replace aggregate stage approval checks with `visual_job_passes()`.
- [ ] Restrict owner `STAGES` and CLI choices to `design-pack`, `package`.
- [ ] Delete states/actions for awaiting standard/direction approval.
- [ ] Implement and test the remaining closed transitions:

```text
producing_directions --all direction verdicts pass--> building_package
building_package --package evidence complete--> awaiting_package_approval
awaiting_package_approval --approve--> complete
awaiting_package_approval --revise package--> building_package
awaiting_package_approval --revise design--> designing
awaiting_package_approval --reject--> rejected
```

- [ ] Add stale-safe actions for every nonterminal state and assert terminal
  states expose an empty action list.
- [ ] Run workflow/CLI tests and commit
  `feat: make production QA agent autonomous`.

### Task 6: Typed Blocked Recovery

- [ ] Write failing tests for blocked records containing prior state, normalized
  key, evidence, and closed recovery alternatives.
- [ ] Implement recovery kinds: `resume-bounded-action`, `change-strategy`,
  `authorize-budget`, `return-to-design`.
- [ ] Remove untyped `clear_blocked`; stale action/revision must be rejected.
- [ ] Add `omnipet recover` with typed `--input name=value` parsing.
- [ ] Map provider-policy blocks to bounded recoveries without retrying.
- [ ] Run GREEN and commit `feat: add typed blocked recovery`.

### Task 7: Evidence-Owner Repair Invalidation

- [ ] Write failing tests for row, prototype, direction, package, and design
  repairs; exact upstream byte preservation; approval invalidation; destination
  state; transaction crash recovery; and immutable audit logs.
- [ ] Generalize the current job graph to artifact owner plus dependency closure.
- [ ] Never move/truncate `attempts.jsonl` or `qa/repair-log.jsonl`; append one
  deterministic repair record after commit.
- [ ] Return design-bound repairs to `designing`, prototypes to `prototyping`,
  rows to `producing_standard_rows`, directions to `producing_directions`, and
  package-only repairs to `building_package`.
- [ ] Run repair/workflow tests and commit
  `feat: invalidate repairs by evidence ownership`.

### Task 8: Independent Final Review

- [ ] Write failing package tests requiring structural plus semantic/identity/
  motion verdicts, direct source-media binding, and one independent visual
  principal different from every package generator and selector principal.
- [ ] Persist package provenance for all contributing selected artifacts.
- [ ] Create engine-owned `qa/package/final-visual-review.json` manifest binding
  the four verdicts and inspected QA media.
- [ ] Reject missing selection provenance, production-agent final reviewer,
  mixed independent principals, owner substitution, and stale media.
- [ ] Add public selection/verdict operations using stale-safe actions.
- [ ] Run package/verdict/CLI tests and commit
  `feat: enforce independent final review`.

### Task 9: Integration Verification

- [ ] Run focused Plan B tests.
- [ ] Run all affected legacy workflow, repair, checkpoint, package, and release
  tests.
- [ ] Run `scripts/test-all.sh`.
- [ ] Run `git diff --check` and inspect status/diff.
- [ ] Commit only test-driven corrections; no empty final commit.

Treat Plan A's package-resource inventory as a required subset. Extend it with
the verdict, attempt, and repair schemas; do not freeze an exact inventory that
later plans are designed to grow.

## Completion Criteria

- Structural success can never substitute for visual semantics, identity, or motion.
- Standard/direction work requires no owner approval and cannot advance on incomplete QA.
- Every provider call is budgeted, attributable, and recurrence-governed.
- Repeated failures force material strategy changes or typed escalation.
- Repairs preserve audit history and invalidate exactly the owned downstream evidence.
- Final review is independently attributable and hash-bound.
