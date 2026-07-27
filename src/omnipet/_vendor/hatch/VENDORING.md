# Vendored hatch-pet engine

The files listed in `MANIFEST.json` were copied from the Codex hatch-pet skill distribution
under the Apache License 2.0. Attribution and modification status
are kept adjacent to the source so the upstream bytes remain exactly reproducible.

OmniPet modified `scripts/assemble_extended_atlas.py` only to replace its
top-level sibling import and `sys.path` mutation with a stable package import.
OmniPet also modified `references/codex-pet-contract.md` to replace the original
installation-specific destination with the public OmniPet `dist/` layout.
OmniPet modified `scripts/extract_strip_frames.py` to reject degenerate component
seeds (one giant blob plus noise) and fall back to stable-slot extraction, so
connected sprite strips are sliced into equal frames instead of producing empty
output.
`MANIFEST.json` records these files as `modified: true` with distinct original and
vendored SHA-256 values. All other source, reference, and license files remain
unmodified.
