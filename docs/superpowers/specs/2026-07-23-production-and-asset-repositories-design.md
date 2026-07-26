# OmniPet Production And Asset Repository Design

## Summary

Separate the OmniPet ecosystem into three repositories with a one-way release boundary:

1. `OmniPet` remains the public workflow engine, validator, repair system, and release tooling.
2. `OmniPet-Production` is one private monorepo containing durable production projects for all official pets.
3. `OmniPets` is one public repository containing only installable, reviewed pet releases and a catalog.

The private repository may contain licensed references, briefs, prompt lessons, approvals, checkpoints, and selected QA evidence. The public asset repository must never contain production prompts, source references, provider accounting, credentials, raw reviewer material, answer keys, local paths, checkpoints, or runtime state.

## Goals

- Make official pet production private by default without hiding the open-source generation engine.
- Publish all official pets through one discoverable, consistent public catalog.
- Keep engine releases independent from binary asset updates and per-pet licensing.
- Eliminate the manual runtime-manifest edits and private function calls needed during SuShi repair.
- Make the production-to-public boundary deterministic, allowlisted, content-addressed, and independently verifiable.
- Keep each public pet's latest release directly usable without installing OmniPet.

## Non-Goals

- Hosting community-owned pets inside the official production repository.
- Supporting arbitrary image providers or package formats as part of this change.
- Publishing production histories or making public asset bundles sufficient to resume generation.
- Storing every historical pet version in the public repository's main branch.
- Designing a graphical production or catalog interface.

## Repository Boundaries

### Public `OmniPet`

OmniPet owns:

- production project schema and migrations
- generation and deterministic hatch operations
- state transitions, repair lifecycle, approvals, checkpoints, and package validation
- sanitized failure diagnostics
- release bundle export and clean-room release verification

OmniPet does not contain the official pet asset catalog. This prevents binary growth, per-pet licenses, and asset review cadence from coupling to Python package and engine releases.

### Private `OmniPet-Production`

Use one private monorepo for all official pets:

```text
OmniPet-Production/
  pets/
    <pet-id>/
      pet.yaml
      brief.md
      references/
      approved/
      prompts/refinements.md
      checkpoint/
      qa/
      dist/
  policies/
    licenses.yaml
    publishing.yaml
  .omnipet/            # ignored runtime
  release-work/        # ignored exported bundles and PR worktrees
```

Commit durable production inputs and accepted decisions when their rights and sensitivity permit it. Keep credentials, raw provider responses, disposable guides, extracted frames, rejected candidates, and ordinary runtime state ignored. Large retained attempts may be archived to private object storage under an explicit retention policy rather than Git.

Special projects may later move to isolated private repositories if their access policy requires it, but the default remains one monorepo to centralize OmniPet upgrades, CI, backups, and publishing policy.

### Public `OmniPets`

Use one public repository for official installable pets:

```text
OmniPets/
  README.md
  LICENSE
  catalog/
    index.json
  pets/
    <pet-id>/
      pet.json
      spritesheet.webp
      preview.webp
      README.md
      LICENSE-ASSETS
      release.json
```

The main branch stores only the latest version of each pet. Git tags and hosted release artifacts retain historical releases. The root license covers catalog code and documentation; every pet declares its own visual asset license using an SPDX identifier and includes `LICENSE-ASSETS`.

## Public Release Contract

`omnipet release export` produces one temporary, allowlisted directory for a single pet. It contains only the files accepted by the public release schema. At minimum:

```text
<pet-id>/
  pet.json
  spritesheet.webp
  preview.webp
  README.md
  LICENSE-ASSETS
  release.json
```

`release.json` is the cross-repository trust record:

```json
{
  "schemaVersion": 1,
  "petId": "sushi",
  "version": "1.0.0",
  "omnipetVersion": "0.2.0",
  "spriteVersionNumber": 2,
  "files": {
    "pet.json": "sha256:<digest>",
    "spritesheet.webp": "sha256:<digest>",
    "preview.webp": "sha256:<digest>",
    "README.md": "sha256:<digest>",
    "LICENSE-ASSETS": "sha256:<digest>"
  },
  "license": "CC-BY-NC-4.0"
}
```

The exact schema is closed and versioned. Export rejects unknown files, symlinks, traversal, missing licenses, stale package approvals, unresolved package warnings, and mismatched hashes. The bundle excludes production manifests, briefs, references, prompts, detailed QA, answer keys, reviewer source verdicts, budgets, provider metadata, checkpoints, local paths, credentials, and runtime files.

`omnipet release verify` consumes only the exported directory. It must not need the production project or provider credentials. It verifies:

- closed `release.json` schema
- all declared file SHA-256 values and no undeclared files
- `pet.json` identity, paths, and `spriteVersionNumber: 2`
- a transparent 1536x2288 WebP atlas with 192x208 cells
- preview file readability
- SPDX license consistency across `release.json`, `LICENSE-ASSETS`, and catalog entry
- portable text with no absolute paths or prohibited production terms

## OmniPet Capability Changes

### Completed-Job Repair Lifecycle

Add a public repair command, for example:

```text
omnipet repair <pet> --job <job-id> --reason <text>
```

It must:

- accept an explicitly named completed or failed visual job
- atomically archive the current source, decoded output, job QA, and relevant metadata
- return the job to pending without modifying upstream accepted jobs
- invalidate all downstream evidence and approvals derived from that job
- reject repair when publication recovery is pending or the invalidation graph is inconsistent
- record a sanitized repair event and reason in run state

The invalidation graph must be defined by OmniPet, not manually inferred by callers. Repairing a standard row invalidates standard aggregate evidence, direction stages, package evidence, and delivery. Repairing a cardinal invalidates cardinal review, both look rows, package evidence, and delivery. Repairing row 9 invalidates row 9 review, row 10, package evidence, and delivery. Repairing row 10 invalidates row 10 review, package evidence, and delivery.

### Controlled Generation Guides

Add a safe API for attaching run-local visual guides to a repair attempt. Each guide record includes a relative path under the run root, SHA-256, role, target job, and whether it is identity-authoritative or layout/pose-only.

Guides must:

- remain in ignored run state
- be snapshotted before a provider request
- be included in attempt provenance
- reject absolute paths, traversal, symlinks, changed hashes, and unsupported image types
- never become package files unless a separate public release schema explicitly allows them

The final row remains one coherent generated row. Guides may direct pose semantics but do not authorize packaging cells assembled from different generation attempts.

### Review Warning Resolution

Replace mutation of generated QA reports with an explicit review-resolution artifact. A resolution contains:

- the path and SHA-256 of the immutable source report
- the exact warning identifiers being resolved
- reviewer identity or stable reviewer label
- pass/fail disposition and substantive note
- hashes of the visual evidence used for review
- creation timestamp and schema version

Package gates treat warnings as resolved only when every current warning has a non-stale passing resolution. Raw metrics and warnings remain unchanged. Any change to the atlas, source report, or evidence makes the resolution stale and restores the block.

### Sanitized Diagnostics

Persist a safe failure category instead of collapsing every exception into `generation-failed`:

- `local-validation`
- `missing-credentials`
- `authentication`
- `authorization`
- `rate-limit`
- `provider-timeout`
- `provider-request`
- `provider-response`
- `deterministic-qa`
- `publication`

Where available, store a sanitized HTTP status, provider request ID, and retryability flag. Never store credentials, prompts, uploaded bytes, provider response bodies, or exception text that may contain sensitive content. `omnipet status` exposes the category and one bounded next action.

### Release Export And Verify

Add public APIs and CLI commands for release export and verification. Export requires current package approval and a passing package check. Verification operates independently and is suitable for public CI. Both commands use atomic directories, closed schemas, SHA-256 binding, and symlink-safe path handling.

## Publishing Workflow

1. A private production project reaches complete workflow state.
2. Private CI runs package check, `release export`, and `release verify`.
3. A scoped publishing bot creates a branch or fork in `OmniPets`.
4. The bot replaces only `pets/<pet-id>/` and deterministically updates `catalog/index.json`.
5. Public CI runs `release verify` from zero trust, checks catalog consistency, scans for prohibited production content, and confirms the PR changes only one pet plus the catalog.
6. A maintainer reviews previews, license, version, and CI results, then merges.
7. Automation creates a pet release tag or repository release record.

The private production repository itself has no general-purpose write credential for the public repository. Only the scoped publishing identity can create a PR. It cannot merge or bypass branch protection.

## Catalog Contract

`catalog/index.json` is generated deterministically from current `pets/*/release.json` records. Each entry includes at least:

- pet ID and display name
- current version
- sprite version
- relative package and preview paths
- SPDX asset license
- file hashes or release record hash

CI rejects duplicate IDs, duplicate paths, version regressions, missing licenses, catalog drift, undeclared files, and release records whose hashes do not match repository content.

## Public Documentation Contract

Both public repositories provide English and Simplified Chinese entry points:

```text
README.md
README.zh-CN.md
```

The two language versions describe the same product boundary and link to each other. They need not be line-for-line translations, but neither may omit security, licensing, installation, or repository-role information present in the other.

The OmniPet READMEs explain that:

- OmniPet is the open-source production, QA, repair, packaging, export, and verification engine.
- It does not bundle the official pet catalog or require users to clone private production projects.
- Users who only want to install pets should visit `OmniPets`.
- Pet creators use OmniPet locally or in a private production repository, then export a sanitized release bundle.

The OmniPets READMEs explain that:

- OmniPets is the public catalog of installable pet assets, not the generation engine.
- Users can browse `catalog/index.json` and `pets/<pet-id>/`, then install `pet.json` and `spritesheet.webp` without a production environment.
- Creators should use OmniPet rather than manually constructing or submitting unverified atlas files.
- Each pet has its own visual asset license and attribution requirements.
- Engine documentation, source, and creator workflow live in the OmniPet repository.

Public CI checks that both README language files exist, contain reciprocal repository links, and reference the current catalog and release commands.

## Failure And Rollback

- Export failure leaves the production project and public repository unchanged.
- PR verification failure blocks merge and exposes only sanitized public errors.
- A publication mistake is reverted in `OmniPets`; private production history remains intact.
- A corrected pet increments its version, exports a new bundle, and replaces the main-branch latest files through a new PR.
- Historical release tags are immutable. A revoked asset remains documented even if its downloadable artifact is removed under policy.
- Publishing automation never deletes private production inputs or runtime backups.

## Security And Privacy

- Enforce allowlists rather than relying only on secret scanning.
- Run secret and absolute-path scans in both private export CI and public PR CI.
- Do not include raw blind-review verdicts, answer keys, or provider accounting in public releases.
- Keep provider keys only in private CI secret storage or local environment variables.
- Require explicit rights and license metadata before export.
- Treat previews as licensed visual assets covered by the pet-specific license.

## Testing Strategy

OmniPet tests must cover:

- complete/failed job repair and the full downstream invalidation matrix
- atomic repair rollback under interruption
- guide path containment, symlink rejection, hash staleness, and snapshot use
- warning resolutions with stale reports, stale visual evidence, incomplete warning coverage, and fail dispositions
- sanitized diagnostic category mapping without sensitive exception leakage
- release allowlist, closed schemas, deterministic hashes, and atomic export
- clean-room release verification without a production project
- atlas geometry, transparency, sprite version, manifest path, preview, and license checks
- prohibited-content and absolute-path detection

`OmniPets` CI tests must cover:

- deterministic catalog regeneration
- one-pet-plus-catalog PR scope
- duplicate IDs and path conflicts
- version regressions
- per-pet license consistency
- release hash verification
- absence of production-only files and sensitive text

## Migration

1. Implement OmniPet repair, warning resolution, and release verification before moving active projects.
2. Create private `OmniPet-Production` and import the retained SuShi production backup as `pets/sushi/`.
3. Normalize SuShi to the production monorepo template, paths, policies, and checkpoint contract so it is equivalent to a project originally created inside `OmniPet-Production`.
4. Create public `OmniPets`, its bilingual READMEs, and its catalog CI.
5. Restore and verify SuShi from its imported checkpoint, run package check, then export it through the new release contract.
6. Compare the exported SuShi package hashes with the current verified package before adding it as `OmniPets/pets/sushi/` and updating the catalog.
7. Do not create or publish an `OmniPet-SuShi` remote. After both migrations verify, retain its original local history only under the private backup policy and remove or archive the standalone working directory.
8. Add `README.zh-CN.md` to OmniPet and add both language README files to OmniPets, with reciprocal links and clear engine/catalog usage guidance.
9. Update OmniPet featured-project documentation so it points to the public OmniPets catalog and no longer implies that production and public repositories share one structure.

## Decisions

- Official assets are centrally maintained.
- Official production uses one private monorepo by default.
- Public assets use a separate `OmniPets` repository, not the OmniPet engine repository.
- Main branch stores only the latest version of each pet; Git tags/releases retain history.
- Visual asset licenses may differ per pet and must be explicit.
- Private CI creates public PRs; public CI independently verifies; a maintainer merges.
- OmniPet and OmniPets both provide English and Simplified Chinese READMEs that explain their reciprocal engine/catalog relationship.
- The standalone local OmniPet-SuShi repository is migration input, not a future public or private repository boundary.
