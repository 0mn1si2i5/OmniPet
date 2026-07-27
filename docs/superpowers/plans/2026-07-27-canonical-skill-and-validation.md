# Canonical Skill And Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship and prove the repository-owned canonical production skill, explicit Phase-1 migration, package parity, and complex/simple two-gate end-to-end workflow.

**Architecture:** Develop the skill as process TDD against frozen fixtures: capture baseline failures without it, write the minimal skill and references, then pressure-test. Package all resources with the engine, migrate old runs only through an explicit atomic transaction, and validate installed-wheel clean-room production and release boundaries.

**Tech Stack:** Python 3.12+, `unittest`, `importlib.resources`, setuptools, existing public OmniPet APIs, deterministic image/reviewer doubles.

---

## Preconditions

Plans A and B are complete. Engine schemas, action contract, two-gate state
machine, Design Pack, visual verdicts, budget/recurrence, provenance, repair, and
release boundaries are stable.

Every commit instruction below is conditional on separate owner authorization.
Otherwise leave verified changes uncommitted.

## File Map

Create:

- `src/omnipet/agent/skill/SKILL.md`
- `src/omnipet/agent/references/design-contract.md`
- `src/omnipet/agent/references/prototype-planning.md`
- `src/omnipet/agent/references/qa-verdicts.md`
- `src/omnipet/agent/references/interaction-and-escalation.md`
- `src/omnipet/agent/references/migration.md`
- `src/omnipet/agent/evaluation.py`
- `src/omnipet/agent/evaluation_runner.py`
- `scripts/run-agent-evaluation.py`
- Four evaluation fixture directories and frozen transcripts.
- `src/omnipet/migration.py`
- `tests/test_agent_skill.py`
- `tests/test_agent_evaluations.py`
- `tests/test_agent_package_data.py`
- `tests/test_phase1_migration.py`
- `tests/test_agent_native_e2e.py`
- `docs/agent-production.md`

Modify package metadata, CLI, checkpoint/approval migration behavior, CI, release
privacy tests, and public docs.

### Task 1: Freeze RED Baseline Scenarios

- [ ] Write failing fixture-loader, runner-protocol, and evaluator tests before
  creating resources.
- [ ] Run RED and record import/runner failure.
- [ ] Define a runner protocol that invokes a configured agent command in a
  temporary frozen workspace. Scenario JSON is supplied on stdin and one closed
  transcript JSON is returned on stdout. Pin model label, permissive-engine
  fixture, tool permissions, budget, and initial state in each scenario.
- [ ] Add `scripts/run-agent-evaluation.py --fixture ID --mode
  baseline|green|pressure`, configured by `OMNIPET_AGENT_EVAL_COMMAND`; fail
  clearly when no runner is configured.
- [ ] Add frozen scenarios:
  `compound-asymmetric-mounted-v1`, `simple-symmetric-blob-v1`,
  `one-sided-prop-v1`, `rigid-front-face-v1`.
- [ ] Define expected risk flags, min/max prototype sets, required/forbidden
  questions, prohibited strategies, QA categories, and escalation outcomes.
- [ ] Add baseline transcripts that demonstrably fail: unsafe mirror, missing
  reverse anchor, over-prototyped blob, or structural-only false success.
- [ ] Produce baseline transcripts by running the configured agent without the
  canonical skill. Record runner/model/config metadata and content digests, then
  sanitize and freeze them. Hand-authored transcripts do not satisfy RED.
- [ ] Implement deterministic closed evaluator; baseline must fail for exact
  expected reasons.
- [ ] Run GREEN and commit `test: freeze agent production baselines`.

Transcript envelope:

```json
{
  "schema_version": 1,
  "fixture_id": "compound-asymmetric-mounted-v1",
  "mode": "baseline",
  "run_number": 1,
  "observed_facts": [],
  "inferences": [],
  "unknowns": [],
  "risk_flags": [],
  "questions": [],
  "prototype_pose_ids": [],
  "strategies": [],
  "qa_categories_used": [],
  "design_pack_sections": [],
  "escalations": [],
  "events": []
}
```

### Task 2: Write Minimal Canonical Skill (GREEN)

- [ ] Write failing static skill tests and green-transcript oracle tests.
- [ ] Run RED: skill/references/transcripts absent.
- [ ] Write `SKILL.md` with contract authority, status resume, intake reasoning,
  complexity classification, adaptive prototypes, Design Pack, production QA,
  recurrence/budget, Final Package, and privacy.
- [ ] Keep schema fields out of prose; link packaged references and schemas.
- [ ] Explicitly prohibit direct workflow/manifest edits, reference-to-nine-row
  skipping, unsupported reverse inference, unsafe mirroring, structural-only
  success, and machine-local skill dependency.
- [ ] Add focused references and smallest passing green transcript per fixture.
- [ ] Produce green transcripts with the same runner, model, permissions, and
  engine fixture; only the canonical skill may differ.
- [ ] Require simple blob to use exactly canonical plus motion-cycle; compound
  mounted fixture uses all matrix-required independent anchors.
- [ ] Run GREEN and commit `feat: add canonical production skill`.

### Task 3: Pressure-Test And Refactor Skill

- [ ] Write failing pressure assertions across three runs per fixture.
- [ ] Combine at least three pressures per run: skip-design request, low budget,
  prior prompt failure, structural-pass semantic-fail, overdesign temptation,
  unsafe mirror temptation.
- [ ] Refactor only clauses exposed by failure; never add pet-specific Napoleon
  facts to the portable skill.
- [ ] Capture every pressure transcript through the runner protocol and bind the
  scenario, skill, engine fixture, model label, permissions, and transcript
  digests so RED/GREEN comparisons are reproducible.
- [ ] Require every safety-critical expectation on every run and >=90% aggregate
  noncritical expectations.
- [ ] Run GREEN and commit `refactor: harden production skill under pressure`.

### Task 4: Package Resource Parity

- [ ] Write failing exact inventory/hash tests for source, editable install,
  wheel, sdist, and wheel rebuilt from sdist.
- [ ] Include skill, references, schemas, fixture JSON, and sanitized transcripts
  through explicit `pyproject.toml` patterns.
- [ ] Test installed resources outside the repository through
  `importlib.resources`.
- [ ] Assert resources are not copied into `pet init` projects or public release
  bundles and no external installer exists.
- [ ] Run GREEN and commit `build: ship canonical agent resources`.

### Task 5: Explicit Atomic Phase-1 Migration

- [ ] Write failing eligibility, result, and injected-crash tests from a frozen
  valid Phase-1 run.
- [ ] Implement source workflow/engine detection, publishing/completed refusal,
  immutable migration manifest, draft unapproved design, all-old-approval
  invalidation, staged validation, atomic swap, and exact rollback.
- [ ] Preserve attempts, budget, repairs, approvals, and provenance only inside
  the migration audit; never promote them to Phase-2 approvals.
- [ ] Add `omnipet migrate-phase1 PET`; no normal command invokes migration
  implicitly.
- [ ] Remove Phase-2 use of `migrate_checkpoint_base_approval`.
- [ ] Run migration/checkpoint/CLI tests and commit
  `feat: add explicit phase-1 migration`.

Migration result must be `designing`, with no Design Pack or package approval.

### Task 6: Complex And Simple End-To-End Fixtures

- [ ] Write failing E2E tests using current action IDs/revisions before every
  mutation.
- [ ] Assert exact sequence:

```text
intake -> designing -> prototyping -> awaiting_design_pack_approval
-> producing_standard_rows -> producing_directions -> building_package
-> awaiting_package_approval -> complete
```

- [ ] Complex fixture: all required anchors, independent left/right, no mirror,
  four verdicts per job, recurrence strategy change, independent final reviewer.
- [ ] Simple fixture: exactly canonical plus motion-cycle and no unnecessary
  questions/prototypes.
- [ ] Use deterministic provider/reviewer doubles producing real readable image
  files and real hash/provenance records; do not bypass validation.
- [ ] Run both from an installed wheel, export public release, copy bundle to an
  empty directory, and verify without project, references, credentials, or run
  state.
- [ ] Run GREEN, add supported Python matrix CI, commit
  `test: validate two-gate agent workflows`.

### Task 7: Documentation And Privacy Contract

- [ ] Write failing docs/privacy tests first.
- [ ] Document repository skill authority, engine precedence, two approval gates,
  clarification vs approval, explicit migration, machine actions, four-category
  QA, and private/public boundaries in Chinese and English.
- [ ] Add representative private design, QA, repair, migration, session, prompt,
  and reference artifacts to release tests; assert none appear by path or content.
- [ ] Update contributor policy: skill changes require evaluation evidence;
  pet-specific facts stay local; fixtures are minimal and anonymized.
- [ ] Run GREEN and commit `docs: describe agent-native production`.

### Task 8: Final Verification

- [ ] Run skill/evaluation/package-resource tests.
- [ ] Run migration tests.
- [ ] Run both named E2E fixtures.
- [ ] Run package/export/privacy tests.
- [ ] Run `scripts/test-all.sh`.
- [ ] Run `git diff --check`, inspect status and diff, and ensure no private
  assets, secrets, machine-local paths, or implicit approval migration.

## Completion Criteria

- Skill behavior has witnessed RED baselines and passing GREEN/pressure evidence.
- Installed distributions contain byte-identical canonical resources.
- Phase-1 migration is explicit, atomic, auditable, rollback-safe, and approval-safe.
- Complex and simple fixtures prove both under-design and over-design prevention.
- Every full run has exactly Design Pack and Final Package owner approvals.
- Public releases exclude all design, QA, repair, migration, and agent-session data.
