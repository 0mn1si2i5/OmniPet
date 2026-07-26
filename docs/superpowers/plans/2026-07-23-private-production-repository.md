# Private OmniPet Production Repository Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create private `OmniPet-Production`, establish its monorepo policy, and migrate SuShi as the first native `pets/sushi` production project.

**Architecture:** The repository contains durable production projects and policy, while ordinary `.omnipet` runtime and release work remain ignored. SuShi is imported from its verified private backup, normalized to the current OmniPet template, restored through supported APIs, and proven to reproduce the already released package hashes.

**Tech Stack:** Git, OmniPet CLI from the completed engine plan, YAML/JSON, shell CI, SHA-256, private object storage policy.

---

### Task 1: Create The Private Monorepo Skeleton

**Files:**
- Create: `README.md`
- Create: `README.zh-CN.md`
- Create: `.gitignore`
- Create: `policies/licenses.yaml`
- Create: `policies/publishing.yaml`
- Create: `scripts/verify-production-repo.sh`
- Create: `.github/workflows/verify.yml`

- [ ] **Step 1: Initialize a local repository without configuring a remote**

Create `/Users/bytedance/Desktop/Zen/OmniPet-Production`, initialize Git, and keep remote creation/private visibility/push as a separately authorized operation.

- [ ] **Step 2: Define ignore and retention policy**

Ignore `.env*`, `.venv`, `.omnipet`, `release-work`, caches, local audit exports, and OS files. Keep pet manifests, briefs, licensed references, approved assets, prompt lessons, checkpoints, and selected accepted QA trackable.

- [ ] **Step 3: Define publishing and license policy schemas**

Require each pet ID to declare allowed SPDX license, public version, catalog eligibility, and rights confirmation. Publishing policy names the OmniPet version pin and release destination but contains no token or credential.

- [ ] **Step 4: Add repository verification**

The script enumerates `pets/*`, runs `omnipet pet validate`, validates policy coverage, rejects credentials/absolute paths in tracked portable files, and confirms ignored runtime is not tracked.

- [ ] **Step 5: Verify and commit**

Run: `scripts/verify-production-repo.sh`

Expected: PASS with zero pets and valid policy schemas.

Commit: `chore: initialize private pet production monorepo`.

### Task 2: Import SuShi Durable Project Files

**Files:**
- Create: `pets/sushi/pet.yaml`
- Create: `pets/sushi/brief.md`
- Create: `pets/sushi/prompts/refinements.md`
- Create: `pets/sushi/approved/canonical-base.png`
- Create: `pets/sushi/checkpoint/**`
- Create: `pets/sushi/qa/**` only for accepted portable evidence
- Create: `pets/sushi/README.md`
- Create: `pets/sushi/LICENSE-ASSETS`
- Modify: `policies/licenses.yaml`

- [ ] **Step 1: Verify the source backup before extraction**

From `OmniPet-SuShi`, run:

```sh
shasum -a 256 -c .local-history/final-production-backup-20260723/SHA256SUMS
git bundle verify .local-history/final-production-backup-20260723/pre-clean-history.bundle
```

Expected: every entry and bundle passes.

- [ ] **Step 2: Extract the pre-clean durable project into staging**

Extract `tracked-tree/before-cleanup.tar` outside the destination. Copy only the current OmniPet production schema files into `pets/sushi`; retain `BUDGET.md`, `AGENT_HANDOFF.md`, old `PROVENANCE.json`, historical tests, and old dependency locks in a private audit archive, not the active pet project.

- [ ] **Step 3: Normalize SuShi to the current template**

Run `omnipet pet init` for a temporary `sushi-template` and reconcile SuShi field-by-field. Add current release metadata for version `1.0.0`, `CC-BY-NC-4.0`, public README, preview, and asset license. Preserve canonical hash `ec292198...e546eb3` and accepted idle hash `8002678...997e40`.

- [ ] **Step 4: Import a current portable checkpoint**

Use the completed final runtime to export a checkpoint through supported OmniPet APIs after the repair/warning-resolution changes. Do not copy raw `.omnipet` paths into the checkpoint. Verify every declared artifact hash before adding it.

- [ ] **Step 5: Validate and commit**

Run:

```sh
omnipet pet validate sushi --repo-root .
omnipet checkpoint restore sushi --repo-root . --force
omnipet status sushi --repo-root .
```

Expected: valid project; restored state is complete or at the final accepted frontier defined by the new checkpoint contract.

Commit: `feat: import SuShi production project`.

### Task 3: Import Private Runtime And Audit Archives

**Files:**
- Create ignored: `.omnipet/runs/sushi/**`
- Create ignored/private external archive manifest: `audit/sushi-retention.json` or configured object-storage record

- [ ] **Step 1: Copy one authoritative runtime**

Use `.local-history/final-production-backup-20260723/runtime/.omnipet`, not both it and the live duplicate. Exclude `.DS_Store`, `.workflow.lock`, and hatch lock files.

- [ ] **Step 2: Reconcile run-root-relative paths**

Use OmniPet migration or restore commands to rewrite repository-relative project selection without flattening `prompts`, `references`, `generated-sources`, `decoded`, `frames`, `qa`, or `final`. Never search/replace absolute paths blindly.

- [ ] **Step 3: Preserve historical archives under retention policy**

Record SHA-256, size, storage URI or local protected path, retention class, and access policy for the 292 MB historical `.omnipet/archives` and old Git bundle. Do not commit large rejected attempts to the monorepo.

- [ ] **Step 4: Verify active runtime**

Run `omnipet status sushi --repo-root .` and `omnipet package sushi --check --repo-root .`.

Expected: complete workflow and passing package check without provider calls.

### Task 4: Resolve Historical Production Policy Explicitly

**Files:**
- Create: `pets/sushi/PROVENANCE.private.json`
- Create: `pets/sushi/COSTS.private.json` only if policy requires tracked private accounting
- Modify: `policies/licenses.yaml`

- [ ] **Step 1: Preserve historical caveats privately**

Carry forward old provenance and cost uncertainty as audit facts. Do not infer that public simplification resolved old records.

- [ ] **Step 2: Record current owner rights decision separately**

Add a dated, explicit rights confirmation for the released asset and the selected CC BY-NC 4.0 license. Keep historical source facts distinct from current distribution authorization.

- [ ] **Step 3: Run policy validation**

Expected: SuShi has complete rights/license fields and no contradictory active release block.

- [ ] **Step 4: Commit**

Commit: `docs: record private SuShi production provenance`.

### Task 5: Export And Compare SuShi Release

**Files:**
- Create ignored: `release-work/sushi-1.0.0/**`
- Modify only if migration defects are found in `pets/sushi/**`

- [ ] **Step 1: Run package verification**

Run:

```sh
omnipet package sushi --check --repo-root .
omnipet release export sushi --repo-root . --output release-work/sushi-1.0.0
omnipet release verify release-work/sushi-1.0.0
```

Expected: all commands pass without provider calls.

- [ ] **Step 2: Compare authoritative package hashes**

Require:

```text
pet.json           842b5726d2d4414ebe1500019a7305c222229ea5a11272c4cc2ec0c66fab67ce
spritesheet.webp   cb6ef89b898f289c9831ffcd2bfcb61ad0bfb9fb5f29af86aa41d9fff8e9a8b7
```

If hashes differ, stop and classify whether canonical JSON or deterministic image encoding changed. Do not silently publish a different package as SuShi 1.0.0.

- [ ] **Step 3: Run complete private repository verification**

Run: `scripts/verify-production-repo.sh`

Expected: PASS with one valid pet and no tracked runtime/secrets.

- [ ] **Step 4: Commit migration corrections if any**

Commit only durable project/policy changes; never commit `release-work` or `.omnipet`.

### Task 6: Authorize Remote Creation Separately

**Files:** None until explicit authorization.

- [ ] **Step 1: Present local verification evidence**

Report repository path, tracked file list, commits, SuShi package hashes, ignored runtime size, and backup locations.

- [ ] **Step 2: Request explicit authorization**

Ask before creating a remote, setting private visibility, adding CI secrets, pushing, or deleting/archiving the standalone `OmniPet-SuShi` directory.
