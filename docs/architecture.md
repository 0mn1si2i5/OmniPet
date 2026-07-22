# Architecture

## Boundaries

OmniPet owns pet validation, built-in OpenAI generation, safe file promotion, run preparation, resumability, approvals, packaging, and CLI reporting. Durable data lives in `pets/<pet-id>`; ignored execution state lives in `.omnipet/runs/<pet-id>`.

The built-in hatch engine owns row definitions, layout guides, extraction, inspection, atlas assembly, direction QA helpers, chroma despill, and v2 validation. Typed dataclass APIs call attributed vendored functions in process. They translate execution failures into hatch API errors, accept regular source files, reject symlink output paths, and never depend on user-installed engine paths.

## Image Generation

Image generation uses the official OpenAI SDK and the allowlisted `gpt-image-2` model. `OPENAI_API_KEY` is read only from the process environment. SDK retries are disabled, snapshots are immutable PNG/JPEG/WebP bytes, and generated PNG files are written atomically beneath run state.

There is no automatic retry. Provider or generation failures persist one failed job and block the workflow until the user explicitly resets it. Aggregate deterministic failures use a separate clear-block action.

Credentials are prohibited by contract in project files, manifests, prompts, logs, and command arguments. Projects may select an allowed quality but cannot configure credentials, custom endpoints, or arbitrary models.

## Data Ownership

Durable project files include the manifest, brief, references, approved canonical base, prompt lessons, selected QA evidence, and final package. Generated prompts, attempts, guides, decoded rows, extracted frames, intermediate atlases, and logs are disposable run state.

Promotion from run state to a durable directory is explicit, validated, and atomic. Paths must remain beneath declared roots; symlinks, traversal, and destination overlap are rejected. Configuration validation is bounded and closed-schema, but it is not an exhaustive secret detector; credentials remain prohibited by contract.

## State Machine

The workflow advances one bounded action at a time. A canonical base approval pause precedes standard generation; standard-row QA and approval precede direction generation; each direction phase pauses for review; package QA and approval precede publication. Checkpoint export stores only accepted, validated artifacts and an actionable frontier. Package only when every hard gate passes.
