# Keeping coding assistants inside a design system

The design system must be an input to the assistant and an independently checked
constraint on its output. A prompt alone cannot provide that guarantee. ADSF's
first source adapter checks authored CSS against the policy in `extensions.css`.

## Working loop

1. Declare the approved tokens and CSS policy in the system file.
2. Generate assistant context and the token stylesheet from that file.
3. Have the coding assistant read the context before editing component CSS.
4. Check the actual source after edits and in CI.
5. Apply exact, unambiguous repairs, then run the same gate again.

```sh
adsys context examples/aurora.design-system.json --output /tmp/design-context.md
adsys tokens examples/aurora.design-system.json --output /tmp/tokens.css
adsys check examples/aurora.design-system.json examples/button.valid.css --input-format css
adsys check examples/aurora.design-system.json examples/button.drift.css --input-format css --format json
adsys fix examples/aurora.design-system.json examples/button.drift.css --input-format css --output /tmp/button.fixed.css
adsys check examples/aurora.design-system.json /tmp/button.fixed.css --input-format css
```

Add the generated context to the project's assistant instructions, and install
the generated stylesheet through the application's normal stylesheet entrypoint.
Run the source gate in independent CI. Protect the approved contract and CI policy
through normal code review; an assistant changing the contract to accept its own
output defeats this workflow.

## CSS policy

`extensions.css` is optional. It has a separate version and public structural
schema at `src/agent_design_system/schemas/css-policy.schema.json`. Core JSON rules
keep their existing semantics. The CSS adapter uses these fields:

- `version`: exactly `0.1.0`.
- `bindings`: explicit token paths mapped to unique CSS custom-property names.
- `properties`: each governed property lists allowed `tokens`, optional literal
  exceptions in `literals`, and `max_values` from one to four (default one).
- `unrestricted_properties`: reviewed exemptions whose values are not governed.
  All other properties are rejected. The reset property `all` is always forbidden.

For example:

```json
{
  "version": "0.1.0",
  "bindings": {
    "color.text.primary": "--text",
    "space.4": "--inset"
  },
  "properties": {
    "color": {"tokens": ["color.text.primary"]},
    "padding": {"tokens": ["space.4"], "max_values": 4, "literals": ["0"]}
  },
  "unrestricted_properties": ["display"]
}
```

An allowed token is property-specific: knowing a color token does not make it
valid for every color role. `color: var(--text)` passes this policy;
`color: #1e293b`, `color: var(--missing)` and `color: var(--text, red)` fail.
`padding: 0 var(--inset)` passes; `padding: 17px` fails.

Component stylesheets cannot define any custom properties. This prevents local
token shadowing and unapproved variable indirection. Priority overrides,
unapproved functions, fallback values and unknown properties are rejected.
Adding visual shorthands or properties to `unrestricted_properties` is an explicit
coverage exemption; review exemptions for alternate ways to set governed styles.

## Repairs and evidence

Exact equivalent hex colors and numeric values with the same unit can be replaced
with an approved `var()` reference. Numeric dimension tokens become pixels;
numeric duration tokens become milliseconds. Token aliases resolve before CSS
serialization. Unsupported token values cause an error rather than an incomplete
stylesheet. A literal matching several allowed tokens requires an explicit choice.
The source adapter never chooses the nearest color or spacing value.

String dimension bindings require `px`, `em`, `rem`, `ex`, `ch`, `vw`, `vh`,
`vmin`, `vmax`, or `%`; duration strings require `s` or `ms`. Number and font-weight
strings must be unitless. The serializer checks balanced syntax and rejects
unsupported units; it does not provide complete browser property validation.

Check never modifies source. Fix changes only proposed value spans, preserves
comments, whitespace and unrelated source bytes, and requires `--output` or explicit `--write`. It rechecks
the output before selecting its exit status. Original reject decisions still fail.
An unresolved value cannot produce a successful repair gate.

Text, JSON and SARIF reports include actual source locations. Reports identify the
inspected file, declaration counts, governed and unrestricted declarations, and a
SHA-256 identity of the canonical system document. Empty coverage rejects. Invalid
or unsupported source exits 2 rather than silently producing a passing inventory.

## Supported source and limits

The parser supports ordinary standalone CSS rules, comments, strings, repeated
declarations, and grouping blocks for `@media`, `@supports`, `@layer`, `@container`,
`@scope`, and `@keyframes`. Nested style rules, imports, escaping, statement
at-rules, and other block at-rules are rejected. It checks declarations, not full
selector/query validity or browser-computed styles.
Grouping depth is limited to 64; excessive nesting produces a controlled source error.

This gate establishes membership in the declared policy for the inspected CSS
files. It does not establish application-wide compliance. It cannot prove that the
app loads the generated token stylesheet, that another stylesheet does not change
those variables, or that JSX, inline styles, CSS-in-JS, Tailwind classes, or runtime
JavaScript follow the contract. It does not enforce component reuse, per-selector
semantic intent, layout quality or accessibility. Those need further source
adapters, app-level coverage and browser verification.

## Project-wide source gate

`adsys project-check` reads an `adsf-css-project` manifest. It discovers every
non-excluded CSS file under `root` (including uppercase `.CSS`), checks the
generated token stylesheet byte for byte, and reports each file's SHA-256 and
declaration count.
The [fixture manifest](../examples/assistant-app.project.json) points to a
small HTML app with two authored CSS files. Its paths are relative to the
manifest directory, except `tokens` and each exclusion path, which are relative
to the scan root. Paths must be canonical and may not escape through `..` or
symlinks. Explicit exclusions require a reason and appear in the report; a
missing exclusion is reported as unused. `.git` is the only built-in exclusion.

```sh
adsys project-check examples/assistant-app.project.json --format json
adsys project-check examples/assistant-app.project.json \
  --expected-sha256 4be9001fef49440bee48e19b1aa938e95d44007960b52df28cdc4310dcf3315a
adsys context examples/aurora.design-system.json \
  --project-manifest examples/assistant-app.project.json \
  --expected-sha256 4be9001fef49440bee48e19b1aa938e95d44007960b52df28cdc4310dcf3315a \
  --output /tmp/assistant-contract.md
```

The second command compares the current design system and manifest with an
independently reviewed fingerprint. The exported assistant context makes this
pinned project command its primary handoff gate and labels the single-file
check as a quick diagnostic. Context export refuses a failed project gate or a
different design system. The CI workflow runs an integration
verifier containing that pinned value. A passing project check means the
discovered CSS and static HTML inventory met this limited contract. It does
not prove browser loading or computed styles. An HTML stylesheet reference
is evidence of a reference, not proof that a browser loaded it.

Each static HTML page must reference the generated token stylesheet. Static
HTML inspection rejects inline style attributes and tags, `<base>` URL
rebasing, duplicate attributes, presentation
attributes, scripts/event handlers, embedded SVG, and external or uninspected
stylesheet references. JS, TS, JSX, TSX, CSS-in-JS candidates, preprocessors,
Svelte, Vue, MDX, Astro, XHTML, and standalone SVG are reported as unverified
rather than silently passed. A project with any such source exits nonzero until an
adapter is added or a reviewed exclusion is declared. The gate remains offline
and read-only; invalid manifests or unsafe paths exit 2, while source drift,
token mismatch, and unverified coverage exit 1.

The policy, manifest, exclusion reasons, expected fingerprint, and CI
invocation all need independent review and branch protection. A fingerprint
stored in editable source cannot stop an assistant from changing both the
contract and its expected value. The HTML inventory is intentionally bounded;
it does not validate arbitrary HTML syntax, runtime DOM changes, utility class
semantics, component reuse, or accessibility. Browser and app-level review are
still required for those claims.
