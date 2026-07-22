# Contributing

OmniPet is alpha software. Open an issue before large changes so scope and compatibility expectations are explicit.

## Development Setup

Requires Python 3.12 or newer.

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
scripts/test-all.sh
```

The suite uses `unittest` and must run without an API key or network access. Do not add paid tests, live provider tests, or tests that read user configuration. Use temporary directories and deterministic fixtures.

## Test-Driven Development

Use test-driven development for behavior changes: write one focused failing test, run it and confirm the expected failure, implement the minimum change, then run the focused and full suites. Documentation and workflow contracts belong in repository tests where they can regress.

## Vendored Code

Do not edit `src/omnipet/_vendor/hatch` casually. Preserve the local `MANIFEST.json` hashes and attribution. Every vendor modification must be recorded as modified in the manifest and explained in `src/omnipet/_vendor/hatch/VENDORING.md`. Review and update the root `NOTICE`, vendored `NOTICE`, license files, package-data rules, and parity tests when importing or modifying vendor material.

## Pull Requests

Keep changes focused, describe the red-green evidence, run `scripts/test-all.sh`, and include documentation for public behavior. Never include credentials, private pet assets, generated run state, or provider responses.
