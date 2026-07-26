# OmniPet

[简体中文](README.md)

> **Alpha (`0.1.0a1`)**: interfaces and project files may change. Inspect generated work before using it in production.

OmniPet is the open-source engine for generating, QAing, repairing, packaging, and releasing v2 desktop-pet sprites. It uses `gpt-image-2` through the official OpenAI SDK and keeps resumable local state, checkpoints, and installable packages separate.

Want to install a pet? Visit the public [OmniPets](https://github.com/0mn1si2i5/OmniPets) catalog. Creators use OmniPet locally or in a private production repository, then export a verified public release bundle.

## Install and start

Python 3.12 or later is required:

```sh
python -m venv .venv
.venv/bin/python -m pip install omnipet
export OPENAI_API_KEY="your-key"
omnipet pet init my-pet
omnipet pet validate my-pet
omnipet hatch my-pet
omnipet status my-pet
```

The OpenAI SDK reads `OPENAI_API_KEY` from the process environment; never commit it. Image requests may incur cost and send prompts and configured reference images to OpenAI, so use only content you are authorized to process.

The first `hatch` creates a base candidate and pauses. Follow `status` for the next approval, QA, or packaging step. Common commands:

```sh
omnipet approve my-pet --stage base --note "identity accepted"
omnipet hatch my-pet
omnipet package my-pet --check
omnipet package my-pet
omnipet release export my-pet --output release-work/my-pet
omnipet release verify release-work/my-pet
```

See the [generation workflow](docs/generation-workflow.md) and [package review](docs/package-review.md) for the complete QA and release flow.

## Working files

```text
pets/my-pet/                 committed project inputs, checkpoints, and final dist/
.omnipet/runs/my-pet/        ignored resumable run state
.omnipet/archives/           ignored archives of replaced run state
```

`.omnipet` is not exclusive to one repository: it can appear wherever OmniPet commands run. Official durable projects normally live in `OmniPet-Production`; `.omnipet` remains local and ignored in production repositories, engine development, and personal projects alike.

## SuShi example

[SuShi v1.0.1](https://github.com/0mn1si2i5/OmniPets/tree/main/pets/sushi) is a complete public release example with verified `pet.json`, sprite atlas, and preview. The public catalog contains installable outputs only; production checkpoints and repair history stay private.

See [CONTRIBUTING.md](CONTRIBUTING.md) for local development.
