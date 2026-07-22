# Package Review Verdict

The package QA command consumes a human-authored verdict tied to the exact artifacts generated for the current run:

```sh
omnipet qa my-pet --stage package --verdict-file package-verdict.json
```

Do not create the verdict until `omnipet hatch my-pet` has built package evidence. Reviewers must inspect `.omnipet/runs/my-pet/qa/package-generated/blind-sheet.png` without seeing `blind-answer-key.json`. Exactly three independent reviewers must classify every pair shown on the sheet. Their names and `pairs` results must be supplied by reviewers, not copied from the hidden answer key.

The root object is closed and contains exactly these fields:

```text
schema_version: 1
stage: "package"
reviewers: three objects with unique non-empty reviewer names and complete pairs arrays
directions: 16 ordered final direction semantic verdicts
direction_evidence: hashes for the direction sheet and final atlas
final_visual: pass verdict, substantive note, and hashes for the blind sheet and final atlas
```

Each item in `direction_evidence` and `final_visual.evidence` has exactly `path` and `sha256`. Compute the hashes from the current run, for example:

```sh
shasum -a 256 .omnipet/runs/my-pet/qa/package-generated/direction-sheet.png
shasum -a 256 .omnipet/runs/my-pet/qa/package-generated/blind-sheet.png
shasum -a 256 .omnipet/runs/my-pet/final/spritesheet-extended.webp
```

`direction_evidence` must list, in order, `qa/package-generated/direction-sheet.png` and `final/spritesheet-extended.webp`. `final_visual.evidence` must list `qa/package-generated/blind-sheet.png` and `final/spritesheet-extended.webp`.

The `directions` array must cover, in order, `000`, `022.5`, `045`, `067.5`, `090`, `112.5`, `135`, `157.5`, `180`, `202.5`, `225`, `247.5`, `270`, `292.5`, `315`, and `337.5`. Every object has exactly `direction`, `expected`, `observed`, `verdict`, and `note`. Both `expected` and `observed` contain `horizontal` and `vertical`; a pass requires the observed axes to equal the expected axes. These final direction semantics and all blind/final visual fields are required.

After authoring the file, run package QA. OmniPet rejects missing fields, extra fields, stale hashes, duplicate reviewers, incomplete blind results, incorrect direction semantics, or any non-pass final visual verdict. Only then approve and publish:

```sh
omnipet approve my-pet --stage package
omnipet package my-pet --check
omnipet package my-pet
```
