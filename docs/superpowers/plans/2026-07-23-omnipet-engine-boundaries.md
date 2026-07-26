# OmniPet Engine Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class repair, controlled guide, warning-resolution, sanitized diagnostic, release export/verify, and bilingual documentation capabilities to OmniPet.

**Architecture:** Keep generation orchestration in `release.py`, but move new cohesive responsibilities into `repair.py`, `diagnostics.py`, `review_resolution.py`, and `public_release.py`. Public release verification is clean-room and consumes only an exported bundle; repair is transactional and invalidates a centrally defined dependency graph.

**Tech Stack:** Python 3.12+, argparse, dataclasses, Pillow, unittest, existing OmniPet atomic JSON/path safety helpers.

---

### Task 1: Add Structured Sanitized Diagnostics

**Files:**
- Create: `src/omnipet/diagnostics.py`
- Modify: `src/omnipet/openai_images.py`
- Modify: `src/omnipet/workflow.py`
- Modify: `src/omnipet/release.py`
- Modify: `src/omnipet/cli.py`
- Create: `tests/test_generation_diagnostics.py`

- [ ] **Step 1: Write failing tests for all safe categories**

Define tests that map missing credentials, 401, 403, 429, timeout, request, malformed response, deterministic QA, and publication failures to the closed categories in the design. Assert persisted JSON contains only `category`, optional integer `status`, optional bounded `request_id`, and `retryable`; assert secret exception text and response bodies are absent.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_generation_diagnostics -v`

Expected: FAIL because `omnipet.diagnostics` and structured blocked diagnostics do not exist.

- [ ] **Step 3: Implement the closed diagnostic value object**

Add a frozen `SafeDiagnostic` dataclass and category allowlist:

```python
DIAGNOSTIC_CATEGORIES = {
    "local-validation", "missing-credentials", "authentication",
    "authorization", "rate-limit", "provider-timeout",
    "provider-request", "provider-response", "deterministic-qa",
    "publication",
}

@dataclass(frozen=True)
class SafeDiagnostic:
    category: str
    status: int | None = None
    request_id: str | None = None
    retryable: bool = False
```

Validate category, status range, request ID length/characters, and credential-like text. Add mapping functions for OpenAI SDK exceptions without storing exception messages or bodies.

- [ ] **Step 4: Persist diagnostics through workflow blocking**

Extend the blocked workflow closed schema with a nullable `diagnostic` object. Pass mapped diagnostics from `openai_images.py` through `JobGenerationError` and `_hatch_project_locked()`. Keep CLI error output fixed and sanitized; expose the diagnostic only through `omnipet status`.

- [ ] **Step 5: Verify focused and regression tests**

Run:

```sh
.venv/bin/python -m unittest tests.test_generation_diagnostics tests.test_openai_images tests.test_workflow tests.test_release_workflow tests.test_end_to_end_cli -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```sh
git add src/omnipet/diagnostics.py src/omnipet/openai_images.py src/omnipet/workflow.py src/omnipet/release.py src/omnipet/cli.py tests/test_generation_diagnostics.py
git commit -m "feat: add sanitized generation diagnostics"
```

### Task 2: Implement Transactional Completed-Job Repair

**Files:**
- Create: `src/omnipet/repair.py`
- Modify: `src/omnipet/release.py`
- Modify: `src/omnipet/run.py`
- Modify: `src/omnipet/workflow.py`
- Modify: `src/omnipet/approvals.py`
- Modify: `src/omnipet/package.py`
- Modify: `src/omnipet/cli.py`
- Create: `tests/test_job_repair.py`
- Create: `tests/test_repair_cli.py`

- [ ] **Step 1: Write the invalidation-matrix tests**

Cover one standard row, `look-cardinals`, `look-row-9`, and `look-row-10`. For each, assert the selected job becomes pending, upstream accepted jobs remain byte-identical, derived jobs become pending, downstream QA/package/delivery files are archived and removed, and approvals truncate at the correct stage.

- [ ] **Step 2: Write transaction rollback and recovery tests**

Patch each archive/move/manifest publication boundary to fail. Assert original files, manifest, approvals, workflow, and delivery marker are restored. Assert repair refuses to run while package or review publication recovery journals exist.

- [ ] **Step 3: Run tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_job_repair tests.test_repair_cli -v`

Expected: FAIL because public completed-job repair does not exist.

- [ ] **Step 4: Define the central invalidation graph**

In `repair.py`, define job-to-job and job-to-stage dependencies from `STANDARD_JOB_IDS` and the three direction jobs. Do not duplicate ad hoc deletion lists in CLI code. Return a `RepairResult` with archived path, repaired job, invalidated jobs, and invalidated stages.

- [ ] **Step 5: Implement atomic repair**

Use `_hatch_lock`, a staging directory beneath `.omnipet/archives/repairs`, atomic `os.replace`, original-byte snapshots for JSON state, and rollback on any exception. Archive source, decoded output, job QA, aggregate QA, affected final/package outputs, approvals, and delivery markers before resetting statuses.

- [ ] **Step 6: Add the public CLI**

Add:

```text
omnipet repair <pet> --job <job-id> --reason <text>
```

Require a bounded non-secret reason and return sanitized JSON containing only IDs, stage names, and run-relative archive path.

- [ ] **Step 7: Verify repair and existing recovery behavior**

Run:

```sh
.venv/bin/python -m unittest tests.test_job_repair tests.test_repair_cli tests.test_release_workflow tests.test_workflow tests.test_release_package tests.test_checkpoint -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```sh
git add src/omnipet/repair.py src/omnipet/release.py src/omnipet/run.py src/omnipet/workflow.py src/omnipet/approvals.py src/omnipet/package.py src/omnipet/cli.py tests/test_job_repair.py tests/test_repair_cli.py
git commit -m "feat: add transactional pet job repair"
```

### Task 3: Add Controlled Run-Local Generation Guides

**Files:**
- Create: `src/omnipet/guides.py`
- Modify: `src/omnipet/release.py`
- Modify: `src/omnipet/run.py`
- Modify: `src/omnipet/checkpoint.py`
- Modify: `src/omnipet/cli.py`
- Create: `tests/test_generation_guides.py`

- [ ] **Step 1: Write failing guide security tests**

Test a closed record with `path`, `sha256`, `role`, `target_job`, and `authority`. Reject absolute paths, traversal, symlinks, changed hashes, wrong target jobs, unsupported formats, credential-like roles, and paths outside run state.

- [ ] **Step 2: Write immutable snapshot and exclusion tests**

Assert provider input uses the snapshotted bytes even if the guide changes afterward. Assert guide metadata, but not bytes, is recorded in attempt provenance. Assert guides never enter checkpoints, package files, or public release exports.

- [ ] **Step 3: Run tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_generation_guides -v`

Expected: FAIL because controlled guides are not implemented.

- [ ] **Step 4: Implement guide registration and validation**

Add a run-local guide registry under `qa/guides.json` with a closed schema. Support `identity`, `pose-only`, and `layout-only` authority values. Copy accepted source images atomically beneath `references/repair-guides/<job-id>/` and bind their hashes.

- [ ] **Step 5: Attach guides to one repair attempt**

Extend `_grounding()` to append validated registered guides for the target pending job. `_begin_attempt()` must snapshot the exact guide records into job attempt metadata before provider invocation. Clear one-shot guides when the attempt completes, fails, or is repaired again.

- [ ] **Step 6: Add CLI arguments**

Support repeatable guide attachment through a separate command or repair options, for example:

```text
omnipet guide add <pet> --job look-row-10 --file guide.png --role "pose sequence" --authority pose-only
```

- [ ] **Step 7: Verify focused and package/checkpoint exclusions**

Run:

```sh
.venv/bin/python -m unittest tests.test_generation_guides tests.test_openai_images tests.test_checkpoint tests.test_release_package -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```sh
git add src/omnipet/guides.py src/omnipet/release.py src/omnipet/run.py src/omnipet/checkpoint.py src/omnipet/cli.py tests/test_generation_guides.py
git commit -m "feat: support controlled repair guides"
```

### Task 4: Implement Immutable Warning Resolutions

**Files:**
- Create: `src/omnipet/review_resolution.py`
- Modify: `src/omnipet/hatch/directions.py`
- Modify: `src/omnipet/package.py`
- Modify: `src/omnipet/approvals.py`
- Modify: `src/omnipet/cli.py`
- Create: `tests/test_warning_resolutions.py`

- [ ] **Step 1: Write failing stable-warning-ID tests**

Require every generated continuity warning to include a deterministic ID derived from report kind, pair/direction, and rule, while preserving human-readable text.

- [ ] **Step 2: Write resolution validation tests**

Test exact warning coverage, duplicate/unknown IDs, stale report hash, stale visual evidence, missing reviewer/note, fail disposition, and byte-for-byte immutability of the source report.

- [ ] **Step 3: Run tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_warning_resolutions -v`

Expected: FAIL because stable IDs and resolution artifacts do not exist.

- [ ] **Step 4: Implement resolution schema and CLI**

Add:

```text
omnipet qa resolve <pet> --report qa/package-generated/continuity.json --verdict-file continuity-resolution.json
```

The resolution binds source report SHA-256, warning IDs, reviewer label, pass/fail, note, visual evidence paths/hashes, and timestamp. Store it separately under `qa/resolutions/`.

- [ ] **Step 5: Update package and approval gates**

`check_package()` accepts warnings only when all current warning IDs have non-stale passing resolutions. Add resolution files to package approval evidence. Any atlas/report/evidence mutation invalidates approval and restores the block.

- [ ] **Step 6: Verify tests**

Run:

```sh
.venv/bin/python -m unittest tests.test_warning_resolutions tests.test_release_package tests.test_workflow tests.test_end_to_end_cli -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```sh
git add src/omnipet/review_resolution.py src/omnipet/hatch/directions.py src/omnipet/package.py src/omnipet/approvals.py src/omnipet/cli.py tests/test_warning_resolutions.py
git commit -m "feat: add review-bound warning resolutions"
```

### Task 5: Add Release Metadata To Pet Projects

**Files:**
- Modify: `src/omnipet/project.py`
- Modify: `src/omnipet/templates/pet/pet.yaml`
- Add: `src/omnipet/templates/pet/LICENSE-ASSETS`
- Modify: `src/omnipet/templates/pet/README.md`
- Modify: `pyproject.toml`
- Modify: `tests/test_project.py`
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: Write failing project-schema tests**

Require a closed `release` block containing semantic `version`, SPDX `asset_license`, public `readme`, asset license file, and preview source. Reject unknown keys, invalid versions, absent license files, traversal, and symlinks.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_project tests.test_packaging -v`

Expected: FAIL because release metadata is absent.

- [ ] **Step 3: Extend `PetProject` and template**

Add validated immutable fields and template defaults suitable for a new private production project. Keep credentials and public-repository credentials out of project schema.

- [ ] **Step 4: Ship new template files**

Include `LICENSE-ASSETS` in setuptools package data. Update installed-distribution tests to assert `omnipet pet init` creates a valid project with release metadata.

- [ ] **Step 5: Verify tests and commit**

Run: `.venv/bin/python -m unittest tests.test_project tests.test_packaging tests.test_cli -v`

Expected: PASS.

```sh
git add src/omnipet/project.py src/omnipet/templates/pet/pet.yaml src/omnipet/templates/pet/LICENSE-ASSETS src/omnipet/templates/pet/README.md pyproject.toml tests/test_project.py tests/test_packaging.py
git commit -m "feat: define pet public release metadata"
```

### Task 6: Implement Clean-Room Release Export And Verify

**Files:**
- Create: `src/omnipet/public_release.py`
- Modify: `src/omnipet/cli.py`
- Modify: `src/omnipet/security.py`
- Modify: `.gitignore`
- Create: `tests/test_release_export.py`
- Create: `tests/test_release_verify.py`
- Modify: `tests/test_end_to_end_cli.py`
- Modify: `tests/test_gitignore.py`

- [ ] **Step 1: Write failing export tests**

Require current package approval/check, exact allowlist, deterministic canonical JSON and hashes, atomic destination replacement, no production files, and rejection of symlinks/collisions/unresolved warnings.

- [ ] **Step 2: Write failing clean-room verify tests**

Verify closed `release.json`, undeclared/missing files, all hashes, pet ID/path/version, real WebP format, 1536x2288 RGBA atlas, preview readability, SPDX consistency, portable text, secret detection, and absence of production terms. Run against a directory with no `pet.yaml`, `.omnipet`, or credentials.

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_release_export tests.test_release_verify -v`

Expected: FAIL because `public_release.py` does not exist.

- [ ] **Step 4: Implement deterministic export**

Export exactly `pet.json`, `spritesheet.webp`, `preview.webp`, `README.md`, `LICENSE-ASSETS`, and generated `release.json`. Stage beneath an ignored `release-work/` parent, fsync, verify, then atomically install the destination.

- [ ] **Step 5: Implement independent verification**

Do not call `load_pet_project()`. Decode images with Pillow and invoke atlas validation against the bundle atlas. Scan bounded UTF-8 text for Unix/Windows absolute paths, credential-like content, and prohibited production filenames/terms.

- [ ] **Step 6: Add CLI commands**

```text
omnipet release export <pet> --output <directory>
omnipet release verify <bundle-directory>
```

Dispatch verify before project loading in `cli.py`.

- [ ] **Step 7: Verify tests**

Run:

```sh
.venv/bin/python -m unittest tests.test_release_export tests.test_release_verify tests.test_end_to_end_cli tests.test_gitignore tests.test_release_package -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```sh
git add src/omnipet/public_release.py src/omnipet/cli.py src/omnipet/security.py .gitignore tests/test_release_export.py tests/test_release_verify.py tests/test_end_to_end_cli.py tests/test_gitignore.py
git commit -m "feat: export and verify public pet releases"
```

### Task 7: Add English/Chinese Engine-Catalog Documentation

**Files:**
- Modify: `README.md`
- Create: `README.zh-CN.md`
- Modify: `docs/architecture.md`
- Modify: `docs/pet-project-format.md`
- Modify: `docs/generation-workflow.md`
- Modify: `tests/test_repository_docs.py`
- Modify: `tests/test_alpha_release.py`

- [ ] **Step 1: Write failing bilingual documentation tests**

Assert both READMEs exist, link to each other, identify OmniPet as engine and `OmniPets` as catalog, document `release export/verify`, preserve security/privacy/license guidance, and do not describe standalone SuShi as the future repository boundary.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_repository_docs tests.test_alpha_release -v`

Expected: FAIL because `README.zh-CN.md` and new relationship text are absent.

- [ ] **Step 3: Update English documentation**

Add a language link at the top, replace the obsolete featured-project statement with the OmniPets catalog relationship, and document creator versus installer paths.

- [ ] **Step 4: Write complete Chinese README**

Include installation, quick start, production privacy, costs, project structure, repair/recovery, release export/verify, troubleshooting, licensing, and links to the OmniPets catalog. Do not reduce it to a short translated summary.

- [ ] **Step 5: Update maintained docs and tests**

Describe production project, checkpoint, `dist`, and public release bundle as distinct objects. Add `README.zh-CN.md` to maintained paths.

- [ ] **Step 6: Verify and commit**

Run: `.venv/bin/python -m unittest tests.test_repository_docs tests.test_alpha_release -v`

Expected: PASS.

```sh
git add README.md README.zh-CN.md docs/architecture.md docs/pet-project-format.md docs/generation-workflow.md tests/test_repository_docs.py tests/test_alpha_release.py
git commit -m "docs: explain OmniPet and OmniPets in two languages"
```

### Task 8: Full Engine Verification

**Files:**
- Modify only if failures reveal implementation defects.

- [ ] **Step 1: Run the complete suite**

Run: `scripts/test-all.sh`

Expected: all unit, integration, packaging, wheel, and source-distribution checks pass.

- [ ] **Step 2: Build and inspect distributions**

Run:

```sh
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

Expected: wheel and sdist build; twine reports `PASSED` for all artifacts.

- [ ] **Step 3: Run clean-room release smoke test**

Create a valid fixture bundle through `omnipet release export`, copy only that bundle to a temporary directory, and run `omnipet release verify` there with `OPENAI_API_KEY` unset.

Expected: verification succeeds and no files are created or modified.

- [ ] **Step 4: Commit verification corrections only when files changed**

Inspect `git status --short`. If verification required corrections, stage only the listed modified implementation or test files by their exact paths and commit them with `git commit -m "fix: complete public release workflow verification"`. If the worktree is clean, do not create an empty commit.
