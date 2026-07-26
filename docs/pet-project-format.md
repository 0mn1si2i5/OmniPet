# Pet Project Format

Each pet is self-contained under `pets/<pet-id>`:

```text
pets/<pet-id>/
|-- README.md
|-- pet.yaml
|-- brief.md
|-- references/
|-- prompts/refinements.md
|-- approved/canonical-base.png
|-- README.zh-CN.md
|-- LICENSE-ASSETS
|-- checkpoint/
|   |-- checkpoint.json
|   |-- artifacts/
|   `-- qa/
|-- qa/
`-- dist/
    |-- pet.json
    `-- spritesheet.webp
```

Create a project with `omnipet pet init <pet-id>`. `pet.yaml` uses `schema_version: 1`; its `id` must match the directory. All paths are relative to the pet root and may not traverse or pass through symlinks.

`style` records portable visual intent. `references` records each source path and role. `image_generation` selects the built-in OpenAI `gpt-image-2` generator and an allowed quality. The validator uses a bounded closed schema but is not an exhaustive secret detector. Credentials are prohibited by contract everywhere and OpenAI credentials are read only from `OPENAI_API_KEY`. The built-in hatch engine requires `hatch_engine.minimum_sprite_version` of at least 2. Package destinations must stay under `dist`.

The brief is the approved identity and motion contract. Refinements record accepted warnings, rejected strategies, and reusable pet-specific prompt lessons. Approved files require explicit user acceptance; generated candidates remain in run state until promoted.

The production project is the full durable creator workspace. Its `release` manifest block declares a semantic version, SPDX asset license, public README paths, `LICENSE-ASSETS`, and preview source. These declarations select inputs for release export; they do not make private project files public.

`checkpoint/` is an optional tracked, portable checkpoint of accepted state. Its closed-schema `checkpoint.json` records the pet and sprite versions, completed jobs, actionable frontier, relative artifact paths and SHA-256 hashes, sanitized accepted provenance, and selected accepted QA paths. Only artifact-validated completed jobs may be exported. Checkpoints must not contain secrets, absolute paths, rejected attempts, unrecognized runtime metadata, raw service responses, or generated prompts that preparation can reconstruct. The directory is distinct from disposable `.omnipet/runs/` and runtime `.omnipet/archives/`.

Checkpoint images inherit the license of the pet assets from which they were produced. OmniPet core provides only the checkpoint transport and validation tooling.

An approval pause separates generated candidates from durable approved files. A failed generation remains in run state with no automatic retry; resetting it requires an explicit CLI action. Restoring a checkpoint verifies every declared path and hash before reconstructing accepted run state.

`dist/` contains the final v2 production package: `pet.json` with `spriteVersionNumber: 2` and `spritesheet.webp` at exactly 1536x2288. It is package output, not a checkpoint and not by itself a public release.

`omnipet release export <pet-id> --output release-work/<name>` creates a separate public release bundle. The closed bundle contains `pet.json`, `spritesheet.webp`, `preview.webp`, `README.md`, optional `README.zh-CN.md`, `LICENSE-ASSETS`, and generated `release.json`. It excludes production manifests, briefs, references, prompts, detailed QA, reviewer source material, budgets, provider data, checkpoints, credentials, absolute paths, and runtime files. `omnipet release verify <bundle-directory>` checks the closed file set, canonical metadata, SHA-256 hashes, identity, atlas, preview, portable text, and license without loading the production project.
