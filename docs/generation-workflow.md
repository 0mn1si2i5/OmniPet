# Generation Workflow

## Preflight

1. Validate the pet project.
2. Confirm the installed OmniPet package can load its built-in hatch runtime and Pillow dependency.
3. Confirm `OPENAI_API_KEY` is set and built-in `gpt-image-2` generation can use local grounding images.
4. Prepare `.omnipet/runs/<pet-id>` and review the visible four-stage status.

The built-in hatch engine is part of the installed package. There is no separately installed runtime.

If any capability is unavailable, stop and report the missing capability. Do not generate faux sprites programmatically with code, SVG, local drawing, cell duplication, or ungrounded placeholders.

## Approval And Generation

1. Generate and obtain user approval for one canonical base. It becomes the identity reference; no animation row starts before approval.
2. Generate the nine standard rows separately with the canonical base and row layout guide attached. Start with `idle` and `running-right`; derive `running-left` by deterministic framewise mirroring only when asymmetric identity and props remain valid.
3. Perform row-level extraction, component checks, and visual motion review immediately after every row. Repair the smallest failing full row.
4. Review the intermediate 8x9 contact sheet and previews, but do not package it.
5. Define natural look mechanics and approve four cardinal anchors: up, screen-right, down, and screen-left.
6. Generate `look-row-9` as one coherent eight-pose family. Register `look-row-9`, then perform its edge review, semantic review, and continuity review.
7. Only after `look-row-9` passes, generate `look-row-10` as one coherent eight-pose family grounded by row 9 and the approved cardinal strip. Immediately review `look-row-10` for registration, edges, semantics, and continuity. Together the two rows form 16 directions; do not rotate the whole sprite or replace isolated final cells.
8. Assemble the 8x11 atlas and perform final consolidated QA: one final chroma despill, v2 validation, full direction semantics and continuity review, three isolated blind axis reviews, and independent final visual QA.
9. Package only when every hard gate passes.

Every named review is an approval pause. Status output identifies the next command; OmniPet never infers approval. Provider calls have no automatic retry. Inspect a failed job before explicitly resetting that same job, and change strategy rather than repeatedly submitting the same failed request.

## Delivery Gate

The installed package must contain both `pet.json` and `spritesheet.webp`. Verify `spriteVersionNumber: 2`, an exact 1536x2288 atlas (8x11 cells of 192x208), nine standard rows plus two look rows, readable cardinal anchors, a coherent clockwise direction loop, and passing engine validation.

Report each stage with artifact paths, passed or failed gates, and the next bounded action. Never claim visual success without inspecting its QA artifact.

## Retention And Recovery

Promote only approved identity assets, selected QA evidence, and verified package files. Keep rejected candidates, preflights, chroma experiments, generated guides, frame caches, and logs in ignored run state.

Use `omnipet status my-pet` after interruption. Correct deterministic failures before regenerating visual work. Repeated root failures require a strategy change, not prompt churn. Never clean up a source until its durable promotion and package checks pass.
