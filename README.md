# OmniPet

> **Alpha software (`0.1.0a1`)**: interfaces and project files may change. Keep source art and checkpoints backed up, inspect every generated image, and expect rough edges.

OmniPet is a guided, resumable workflow for generating and packaging v2 animated desktop pets. One Python package supplies OpenAI image generation, the built-in hatch engine, deterministic image tooling, approval gates, QA, checkpoints, and final package validation. No separate runtime installation is needed.

## What Works

- Prompt-only or reference-grounded pet projects with durable briefs and manifests.
- Built-in `gpt-image-2` generation through the official OpenAI SDK.
- A canonical-base approval followed by nine standard animation rows and 16 look directions.
- Row extraction, previews, direction checks, one final chroma despill, and 1536x2288 v2 atlas validation.
- Explicit QA and approval pauses, resumable local run state, portable checkpoints, and atomic publication of `pet.json` and `spritesheet.webp`.

## Install

OmniPet requires Python 3.12 or newer.

```sh
python -m venv .venv
.venv/bin/python -m pip install omnipet
.venv/bin/omnipet --version
export OPENAI_API_KEY="your-key"
```

`OPENAI_API_KEY` is read only from the process environment. Do not put it in `pet.yaml`, prompts, shell history, issue reports, or checkpoints.

## Quick Start

`omnipet pet init` uses the template shipped in the installed distribution and creates an immediately valid prompt-only project. Add a real reference for best identity quality:

```sh
omnipet pet init my-pet
mkdir -p pets/my-pet/references
cp /path/to/your-character.png pets/my-pet/references/your-character.png
omnipet pet validate my-pet
omnipet hatch my-pet
omnipet status my-pet
```

Edit `pets/my-pet/pet.yaml` and `brief.md` before hatching. The first hatch call creates one base candidate and pauses. Inspect the paths in status output, then continue through the explicit stages:

```sh
omnipet approve my-pet --stage base --note "identity accepted"
omnipet hatch my-pet
omnipet qa my-pet --stage standard-rows --verdict-file standard-verdict.json
omnipet approve my-pet --stage standard-rows
omnipet hatch my-pet
omnipet qa my-pet --stage directions --verdict-file direction-verdict.json
omnipet approve my-pet --stage directions
omnipet hatch my-pet
omnipet qa my-pet --stage package --verdict-file package-verdict.json
omnipet approve my-pet --stage package
omnipet package my-pet --check
omnipet package my-pet
omnipet checkpoint export my-pet
```

Direction QA is phased: cardinals, look row 9, then look row 10. Follow `omnipet status my-pet` after every command; it reports the next bounded action. Approval pauses are intentional and OmniPet does not retry generation automatically. A failed provider job remains blocked until you inspect it and explicitly run `omnipet hatch my-pet --reset-failed <job-id>`.

Package QA requires three independent blind reviews, a 16-direction final semantic review, and a final visual review bound to the generated artifacts by path and SHA-256. Create `package-verdict.json` from the evidence produced by the preceding hatch step; see [`docs/package-review.md`](docs/package-review.md) for the closed JSON contract and review workflow. Do not reuse example hashes or record a pass before completing the reviews.

## Costs And Privacy

OpenAI image requests may incur costs. OmniPet cannot predict or cap provider charges, so review the provider's current published rates and account limits before hatching. Each accepted workflow normally requires multiple image requests; failed work is not retried automatically.

Prompts and configured reference images are sent to OpenAI. Review OpenAI's current data and privacy terms, and use only images you have the right and consent to process. Local run state may contain generated images and sanitized service metadata. Credentials are prohibited by contract, but validation is a bounded closed schema and not an exhaustive secret detector.

## Project Structure

```text
pets/my-pet/                 durable source, approvals, checkpoint, package
|-- pet.yaml
|-- brief.md
|-- references/
|-- approved/
|-- checkpoint/
`-- dist/
.omnipet/runs/my-pet/        ignored, resumable working state
.omnipet/archives/           replaced run archives
```

Commit briefs, licensed references, approved identity assets, refinement decisions, selected QA evidence, checkpoints, and final package files. Do not commit `.omnipet/`, rejected attempts, generated guides, frame caches, logs, credentials, or local environments.

## Recovery

Start with `omnipet status my-pet`. Correct deterministic failures without regenerating accepted visual work. Use `--reset-failed <job-id>` only for a failed generation job and `--clear-block` only after correcting an aggregate non-job failure.

Export accepted work with `omnipet checkpoint export my-pet`. Restore it with `omnipet checkpoint restore my-pet`; pass `--force` only when you intend to archive and replace existing run state. If disposable state is irreparable and no checkpoint exists, remove only `.omnipet/runs/my-pet` and hatch again. Never delete approved source assets as a recovery step.

## Troubleshooting

- `invalid pet project`: run `omnipet pet validate my-pet`, then check IDs, relative paths, YAML fields, and symlinks.
- `hatch failed` or blocked status: inspect the reported job and QA evidence. OmniPet deliberately hides provider details that may contain sensitive data.
- Missing key: export `OPENAI_API_KEY` in the same shell that runs OmniPet.
- Package failure: complete all QA and approval gates, then run `omnipet package my-pet --check` before publication.
- Interrupted process: run `omnipet status my-pet`; accepted run artifacts are designed to resume.

## Current Limitations And Roadmap

The alpha supports one built-in image model and one package format. It has no graphical interface, arbitrary model selection, custom endpoint support, unattended approvals, automatic retries, or provider cost accounting. Visual verdict files still require careful human preparation.

The roadmap is driven by alpha feedback: clearer verdict authoring, stronger visual review ergonomics, migration tooling for schema changes, and broader platform verification. These are directions, not compatibility promises.

See `docs/architecture.md`, `docs/pet-project-format.md`, and `docs/generation-workflow.md` for the maintained contract.

For development, install the local package with `.venv/bin/pip install -e '.[dev]'` and run `scripts/test-all.sh`.

## Featured Project

`OmniPet-SuShi` demonstrates the workflow in an independent project. Its visual release is blocked pending rights confirmation, so no character assets are bundled here.
