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
The Aurora fixture does not exempt `content`: generated `::before` or `::after`
copy can change visible labels without changing DOM `textContent`. Another
design system may explicitly allow it, but that system must review and verify
the resulting visible copy.

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
generated token stylesheet byte for byte, and reports a SHA-256 and declaration
count for each successfully inspected stylesheet. Rejected inputs have named
issues.
The manifest and system are trusted only at their exact declared paths; all
other regular files receive an explicit outcome. Non-CSS/HTML files fail with
`adapter-required` and a path, size, and streamed SHA-256 in the coverage
report. Files over 64 MiB retain their path and size but show an explicit
`size-limit` hash status. This includes JSX, JSON, Markdown, extensionless
files, and media assets. An image can itself carry off-brand design, so a
suffix alone is not an automatic exemption. Keep the root narrow and declare
reviewed exclusions for assets or generated output that this adapter cannot
inspect.
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
attributes, scripts/event handlers, embedded SVG, inert `<template>` or
`<noscript>` content, and external or uninspected
stylesheet references. It also checks every static `class` token against nonempty,
simple top-level `.class` rules in CSS directly linked by that page. A selector in a
comment, conditional group, complex selector or unrelated stylesheet does not
count as a base rule. Alternate, disabled, media-qualified, titled or typed
stylesheet links cannot establish loading and fail the gate. This is static
linkage evidence, not proof of browser loading, computed styles, or selector
matching at runtime. A project with any uninspected source or asset exits
nonzero until an adapter is added or a reviewed exclusion is declared. The gate
remains offline and read-only; invalid manifests or unsafe paths exit 2, while
source drift, token mismatch, and unverified coverage exit 1. CSS and HTML
inputs over 8 MiB, directory nesting beyond 64 levels, and scans over 20,000
entries fail closed.

The policy, manifest, exclusion reasons, expected fingerprint, and CI
invocation all need independent review and branch protection. A fingerprint
stored in editable source cannot stop an assistant from changing both the
contract and its expected value. Excluded directory contents remain outside the
gate and can change without changing the fingerprint; the report identifies
those holes but does not approve their contents. The HTML inventory is bounded;
it does not validate arbitrary HTML syntax, runtime DOM changes, utility class
semantics, component reuse, or accessibility. Browser and app-level review are
still required for those claims.

## Optional static TSX source profile

The project manifest may add a `tsx` object with `profile: "static-jsx-v1"`
and reviewed SHA-256 values for the adapter script and package lock. Install the
optional parser from a source checkout before running that gate, and select a
trusted absolute Node executable outside the assistant's project `PATH`:

```sh
npm ci --prefix src/agent_design_system/adapters/tsx --ignore-scripts --no-audit --no-fund
export ADSF_NODE_BIN="$(command -v node)"
python -m agent_design_system project-check PATH/TO/project.json --format json
python scripts/verify-tsx-workflow.py
```

The manifest fields are `adapter_sha256` and `lock_sha256`; calculate reviewed
values with `shasum -a 256` on `check.cjs` and `package-lock.json` in the adapter
directory, then hold the resulting manifest/system fingerprint outside agent
write access. The gate compares adapter and lock bytes with the manifest, and
installed TypeScript package and entrypoint bytes with reviewed package hashes,
before launching Node. CI pins adapter and lock hashes independently in the TSX
workflow verifier.
The source checkout's `npm ci` location is required: a wheel includes the
sidecar and lock, but not `node_modules` or a writable parser-install directory.

The sidecar uses TypeScript 6.0.3 from an exact npm lock and checks syntax with
the [TypeScript Compiler API](https://github.com/microsoft/TypeScript/wiki/Using-the-Compiler-API).
It parses source as data; it does not execute project code, use the network, or
invoke a model. Python remains the policy authority and validates the sidecar's
protocol, parser version, source hash and response shape. A missing or wrong
parser, timeout, malformed result, hash drift or parse diagnostic fails closed.
Reports include source, adapter and lockfile hashes. The core Python package remains
dependency-free when TSX is not enabled.

The accepted file profile has only relative, side-effect `.css` imports and one
named, default-exported, parameterless function with a single return of static
JSX. It requires an exact generated-token CSS import and a `className` on every
JSX element. Every literal `className`
token must have a nonempty simple base rule in CSS imported by that file and
inspected by the project gate. JSX accepts only a small set of ordinary HTML
tags and inert quoted attributes. It rejects arbitrary statements, runtime
expressions, spreads, custom components, inline styling, event/ref props,
style-capable tags, JavaScript/TypeScript imports, compiler pragmas, and entity
or escape spellings that could change a class name. These restrictions follow
from [JSX's support for expressions, spreads and custom components](https://www.typescriptlang.org/docs/handbook/jsx.html),
which would otherwise leave executable styling outside this profile.

A passing TSX result means only that the inspected static component source met
this profile. The project gate still rejects ordinary JavaScript/TypeScript and
does not verify a React entrypoint, imported component graph, bundler output,
browser loading, computed styles or runtime mutations. Work toward a real app
PASS must close those paths and verify the built behavior separately.

## Advisory Tailwind and TSX audit

`tailwind-audit` is a read-only inventory for a Tailwind app that cannot use the
restrictive static TSX profile above. It scans direct `.tsx` files under a root,
literal and enumerable JSX `className`/`class` tokens, and one flat authored
`@theme` block. It compares theme hex values and arbitrary hex utilities with a
JSON canon array, reports Tailwind default-palette utilities, and records dynamic
class expressions as explicit coverage gaps. It neither imports app code nor
loads a model. The optional exact TypeScript 6.0.3 parser and trusted Node path
are installed as described above.

```sh
export ADSF_NODE_BIN="$(command -v node)"
python -m agent_design_system tailwind-audit \
  --tsx-root PATH/TO/src --theme-css PATH/TO/src/index.css \
  --canon PATH/TO/design/canon.json --canon-path swatches.web.hexes \
  --exclude '__tests__/*' --format json > tailwind-audit.json
```

An audit with findings exits zero unless `--fail-on-findings` is supplied.
Input, parser, and protocol failures exit 2. The JSON report includes exact
source hashes, counts, up to 5,000 location-bearing findings, and a truncation
count. It does not inspect CSS outside the specified `@theme`, other file types,
runtime class values, generated markup, visual output, or whether a token is
appropriate for its role. An audit result is therefore never an app-wide pass.
Keep it advisory until the coverage and policy are reviewed for the app.

## Rendered React fixture

The optional fixture in `examples/rendered-react-source/` takes the next narrow
step. Its app-authored `index.html` and `main.tsx` are pinned by exact reviewed
SHA-256 values in the project manifest; `App.tsx` imports only the generated
token sheet and two authored CSS files. The project gate checks the entry and
host pins, statically inspects `App.tsx` and the CSS, and verifies class-to-rule
links. A separately pinned tool-owned build recipe bundles the inspected app
entry with locked esbuild/React versions and accepts only the six inspected or
pinned project inputs plus the locked React runtime.
It requires exactly `index.html`, `app.js`, and `app.css` as outputs. A second
build from a different source path must produce identical bytes. Both the
source and output inventories fail if another file appears, and the verifier
tests source-byte, built-CSS-byte, and extra-output mutations.

To reproduce locally from this source checkout, install the static TSX parser
as above in an environment with this Python package installed (for example,
`python -m pip install -e '.[dev]'`), then select trusted absolute Node and npm
CLI paths and run:

```sh
export ADSF_NODE_BIN="$(realpath "$(command -v node)")"
export ADSF_NPM_CLI="$(realpath "$(command -v npm)")"
npm ci --prefix scripts/react-fixture --ignore-scripts --no-audit --no-fund
python scripts/verify-rendered-react-workflow.py --output /tmp/adsf-rendered-fixture --attestation /tmp/adsf-rendered-attestation.json
node scripts/react-fixture/browser-verify.cjs /tmp/adsf-rendered-fixture /tmp/adsf-rendered-attestation.json
```

The Python verifier installs the fixture's exact npm lock in a new temporary
directory with scripts disabled; it never runs a project-provided build script
or configuration. Use a fresh output path. The browser command needs Chromium
installed with `node scripts/react-fixture/node_modules/playwright-core/cli.js
install chromium`, or set `ADSF_CHROME_BIN` to a trusted Chrome executable. CI
installs the fixture tools and Chromium and runs both checks on Python 3.11.

The Python verifier writes the artifact hashes in a fresh attestation outside
the output directory. Keep that attestation and the verifier outside the
assistant's writable scope. The app profile requires an external expected
contract fingerprint, so a caller cannot pass by changing manifest pins in
the same checkout. The browser verifier checks that attestation, serves only
the attested bytes from a local three-path server, blocks unknown and external
requests, and checks that Chromium mounted one visible button, loaded the CSS,
retained the approved token, and computed the approved button foreground and
background.
It also rejects generated `::before` and `::after` content on the rendered page.
An injected CSS response that adds pseudo-element text must fail that assertion;
this closes a local false pass where the screenshot showed extra button copy
while DOM `textContent` and the previous color checks remained unchanged.
For the controlled approved edit, append `--expect-approved-edit-v1` to require
the exact “Review now” text and 16px top and bottom padding; the baseline must
fail that mode. It then replaces the CSS *response* with a red override while
leaving source and built files unchanged; the same approved-style assertion
must reject that response. This
is a probe-sensitivity test, not a claim that every visual property was checked.
The separate `serve.py` script is a manual preview helper and does not attest
asset hashes. A standalone browser pass depends on the trusted Python verifier
and attestation handoff; a caller-controlled attestation can re-label changed
assets. The reviewed host and entry are fixed for this profile; it does not
accept arbitrary entry changes, component graphs, routes, server code, or
interactions. A compliant App/CSS edit can pass with new artifact hashes: a
local trial changed the button text to “Review now” and token-based top padding
to 16px, and Chromium observed both. This was a controlled edit, not yet a
blind coding-assistant trial.
The local result does not establish a remote GitHub CI run. A real app gate
also needs a protected policy, verifier, and expected fingerprint so an
assistant cannot silently change both the contract and its check.
