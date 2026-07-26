# Public OmniPets Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create public `OmniPets`, implement a deterministic latest-version catalog, add bilingual user documentation, and import SuShi through a verified release bundle.

**Architecture:** `OmniPets` contains no production state. Each `pets/<id>` directory is a verified release bundle plus pet-specific documentation/license; `catalog/index.json` is generated from closed `release.json` records. Public CI independently runs OmniPet clean-room verification and enforces one-pet-plus-catalog PR scope.

**Tech Stack:** Git, JSON, shell/Python validation scripts, OmniPet `release verify`, GitHub Actions or equivalent public CI.

---

### Task 1: Initialize The Public Catalog Repository

**Files:**
- Create: `README.md`
- Create: `README.zh-CN.md`
- Create: `LICENSE`
- Create: `.gitignore`
- Create: `catalog/index.json`
- Create: `scripts/build-catalog.py`
- Create: `scripts/verify-repository.py`
- Create: `.github/workflows/verify.yml`
- Create: `tests/test_catalog.py`

- [ ] **Step 1: Initialize locally without a remote**

Create `/Users/bytedance/Desktop/Zen/OmniPets` and initialize Git. Do not create a remote, push, or publish yet.

- [ ] **Step 2: Write failing empty-catalog tests**

Test closed catalog schema, deterministic ordering, no duplicate IDs/paths, and exact regeneration from `pets/*/release.json`.

- [ ] **Step 3: Run tests and confirm failure**

Run: `python -m unittest tests.test_catalog -v`

Expected: FAIL because scripts and catalog do not exist.

- [ ] **Step 4: Implement deterministic catalog builder**

Canonical JSON uses UTF-8, sorted pet IDs, two-space indentation, and one trailing newline. Derive entries only from verified release records; never permit hand-edited catalog fields that disagree with a pet release.

- [ ] **Step 5: Implement repository verification**

Verify all pet directories with `omnipet release verify`, compare generated catalog bytes, reject undeclared files, production terms, absolute paths, symlinks, missing per-pet licenses, and version regressions against the merge base when available.

- [ ] **Step 6: Verify and commit**

Run: `python -m unittest tests.test_catalog -v && python scripts/verify-repository.py`

Expected: PASS with an empty catalog.

Commit: `chore: initialize OmniPets catalog`.

### Task 2: Write Reciprocal English And Chinese READMEs

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Create: `tests/test_docs.py`

- [ ] **Step 1: Write failing documentation parity tests**

Require reciprocal language links; links to OmniPet; statements that OmniPets is the asset catalog and OmniPet is the engine; installer instructions; creator release workflow; catalog path; per-pet license warning; and no implication that production repositories are public.

- [ ] **Step 2: Write English README**

Cover browsing `catalog/index.json`, installing `pet.json` and `spritesheet.webp`, latest-version policy, per-pet licenses, and links to OmniPet creator documentation.

- [ ] **Step 3: Write Chinese README**

Provide equivalent complete content for Chinese users, including install/use steps and the relationship to OmniPet. It may be idiomatic rather than line-for-line translated.

- [ ] **Step 4: Run tests and commit**

Run: `python -m unittest tests.test_docs -v`

Expected: PASS.

Commit: `docs: add bilingual OmniPets usage guide`.

### Task 3: Define Public PR Scope And CI

**Files:**
- Create: `scripts/verify-pr-scope.py`
- Modify: `.github/workflows/verify.yml`
- Create: `tests/test_pr_scope.py`

- [ ] **Step 1: Write failing PR-scope tests**

Accept changes under exactly one `pets/<id>/` plus `catalog/index.json`. Reject changes to another pet, workflow/scripts in an asset PR, root licenses/docs, deleted unrelated files, and catalog-only drift.

- [ ] **Step 2: Implement scope verifier**

Parse changed paths passed by CI rather than invoking shell parsing. Return a fixed sanitized error and the conflicting path classes.

- [ ] **Step 3: Configure public CI**

CI installs a pinned released OmniPet verifier, runs repository verification, docs tests, catalog tests, and PR scope checks. It uses no provider key or private-repository credential.

- [ ] **Step 4: Verify and commit**

Run: `python -m unittest tests.test_pr_scope -v`

Expected: PASS.

Commit: `ci: verify isolated pet release changes`.

### Task 4: Import SuShi From The Private Export

**Files:**
- Create: `pets/sushi/pet.json`
- Create: `pets/sushi/spritesheet.webp`
- Create: `pets/sushi/preview.webp`
- Create: `pets/sushi/README.md`
- Create: `pets/sushi/README.zh-CN.md`
- Create: `pets/sushi/LICENSE-ASSETS`
- Create: `pets/sushi/release.json`
- Modify: `catalog/index.json`

- [ ] **Step 1: Verify the source release bundle before copying**

Run: `omnipet release verify /Users/bytedance/Desktop/Zen/OmniPet-Production/release-work/sushi-1.0.0`

Expected: PASS in clean-room mode.

- [ ] **Step 2: Copy only declared release files**

Do not copy `.gitignore`, production README, `ATTRIBUTION.md` containing private history, prompts, QA, checkpoints, or runtime. If public attribution is required, include it through the release README/license contract.

- [ ] **Step 3: Verify authoritative hashes**

Require the exported `pet.json` and `spritesheet.webp` hashes to equal the verified SuShi release hashes. Verify `preview.webp` and public docs against `release.json`.

- [ ] **Step 4: Regenerate catalog**

Run: `python scripts/build-catalog.py --check` first and expect failure due to stale empty catalog. Then run `python scripts/build-catalog.py` and inspect the single SuShi entry.

- [ ] **Step 5: Run all public verification**

Run:

```sh
omnipet release verify pets/sushi
python scripts/verify-repository.py
python -m unittest discover -s tests -v
```

Expected: PASS; no production-only terms or files.

- [ ] **Step 6: Commit**

Commit: `feat: publish SuShi v1.0.0`.

### Task 5: Test The Bot PR Workflow Locally

**Files:**
- Modify only if workflow defects are found.

- [ ] **Step 1: Create a disposable branch from main**

Replace `pets/sushi` with the same verified bundle and regenerate the catalog. Assert the resulting diff is empty, proving idempotence.

- [ ] **Step 2: Simulate a version update**

Use a fixture release `1.0.1`, verify only `pets/sushi/**` and `catalog/index.json` change, and ensure the PR scope checker passes.

- [ ] **Step 3: Simulate prohibited changes**

Add `brief.md`, `.omnipet`, an absolute path, or a second pet change. Ensure public CI scripts reject each case.

- [ ] **Step 4: Run complete verification**

Run: `python scripts/verify-repository.py && python -m unittest discover -s tests -v`

Expected: PASS on the clean main tree.

### Task 6: Authorize Public Remote Separately

**Files:** None until explicit authorization.

- [ ] **Step 1: Present local evidence**

Report commit history, tracked tree, SuShi hashes, catalog entry, bilingual README links, CI commands, and clean status.

- [ ] **Step 2: Request explicit authorization**

Ask before creating the public remote, pushing, configuring branch protection, creating tags/releases, or setting the publishing bot identity.
