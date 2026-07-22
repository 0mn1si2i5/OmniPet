# Pet Project

The template is immediately valid for prompt-only generation. For the best identity quality,
add a cleared character reference before generation:

```sh
omnipet pet init <pet-id>
mkdir -p pets/<pet-id>/references
cp /path/to/your-character.png pets/<pet-id>/references/your-character.png
```

Add its path and role to `references` in `pet.yaml`. Change `id`, display metadata, style,
image generation quality, brief, and refinements for the pet. Then run
`omnipet pet validate <pet-id>`.

Keep the brief, original references, approved canonical base, refinements, portable `checkpoint/`, selected QA, and final package durable. Keep generated work under `.omnipet/runs/<pet-id>`. Export accepted state with `omnipet checkpoint export <pet-id>` (or selector `.` in a standalone repository); checkpoint visual assets inherit the pet asset license.
