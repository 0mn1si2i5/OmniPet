# Contributing

OmniPet is an early project. Keep changes small and describe the user-visible behavior they change.

## Local development

Python 3.12 or newer is required:

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
scripts/test-all.sh
```

Tests cover executable behavior and structured formats. Update documentation when behavior changes, but prose is not a test contract.

Do not commit API keys, private pet assets, provider responses, or `.omnipet` runtime state. Changes to vendored Hatch code also need its manifest and attribution updated.
