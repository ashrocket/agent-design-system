# Agent Design System Format (ADSF)

ADSF is an open format for design systems that both agents and ordinary test
tools can understand. It pairs explicit design tokens with deterministic rules
and decision trees. The reference `adsys` CLI can report violations, propose or
apply safe coercions, reject unsafe values, emit SARIF, and keep an append-only
audit log—without calling a model.

The repository also includes an optional, budget-gated nightly research harness.
It asks one small question about the format, records evidence and proposals for
human review, and refuses to start when daily, weekly, or monthly token
reservations are exhausted.

Status: early alpha. The core mechanics work and are tested; ADSF 0.1 is a
proposal, not yet a community standard.

## Why this shape

Design-system guidance is often split between prose, code, token files, and
reviewer intuition. An agent can read those sources but two agents may make
different choices. ADSF moves enforceable choices into data:

```text
selected value -> assertion passes -> accept
                         |
                         v
                  first matching branch
                    /    |      \
                report coerce  reject
```

The model can help improve the contract. It is never required to enforce it.

## Quick start

Python 3.11 or newer is required. The runtime has no third-party dependencies.

```sh
python -m pip install -e .

adsys validate examples/aurora.design-system.json
adsys check examples/aurora.design-system.json examples/button.valid.json
adsys check examples/aurora.design-system.json examples/button.invalid.json --format json

adsys fix \
  examples/aurora.design-system.json \
  examples/button.invalid.json \
  --output /tmp/button.fixed.json
```

The invalid example produces three deterministic token coercions and one
accessibility report. The fix command writes a copy; use `--write` only when an
in-place edit is intentional.

For CI and code-scanning integrations:

```sh
adsys check system.adsf.json component.json --format sarif --fail-on warning > adsys.sarif
adsys check system.adsf.json component.json --log .adsys/violations.jsonl
```

An independently implemented enforcer can run the normative vectors with:

```sh
adsys conformance
```

The vectors compare exact violation ordering, dispositions, replacements, fixed
documents, threshold behavior, token-alias resolution, and numeric tie-breaking.
See [docs/conformance.md](docs/conformance.md) for the portable manifest contract.

## Format at a glance

```json
{
  "format": "adsf",
  "version": "0.1.0",
  "name": "Example",
  "description": "Design decisions an agent can explain and a tool can enforce.",
  "tokens": {
    "color": {
      "action": {
        "primary": {
          "$type": "color",
          "$value": "#2563eb",
          "$description": "Primary interactive action."
        }
      }
    }
  },
  "rules": [
    {
      "id": "background-color-token",
      "description": "Backgrounds use color tokens.",
      "rationale": "Semantic tokens preserve themeability.",
      "selector": "$.components.*.style.background",
      "assert": { "operator": "token-reference", "category": "color" },
      "severity": "error",
      "decision": [
        {
          "when": { "operator": "coercible", "strategy": "nearest-token", "category": "color" },
          "action": { "type": "coerce", "strategy": "nearest-token", "category": "color" }
        },
        {
          "when": { "operator": "always" },
          "action": { "type": "report" }
        }
      ]
    }
  ]
}
```

ADSF has three complementary contracts: the JSON Schema defines structure,
[docs/format.md](docs/format.md) defines semantics, and the packaged conformance
suite defines observable behavior. A tool is conforming only when all three
agree. The structural schema lives at
[src/agent_design_system/schemas/design-system.schema.json](src/agent_design_system/schemas/design-system.schema.json).

## Nightly research without runaway spend

Preview the next run for free:

```sh
adsys research run --dry-run
adsys research status
```

Run once using a locally authenticated Codex CLI:

```sh
adsys research run
```

Budget behavior is conservative:

- planning happens before Codex starts;
- one run per UTC day by default;
- daily, calendar-week, and calendar-month gates all have to pass;
- cooldowns rotate the research questions;
- each attempt is charged at least its reservation, even if it fails;
- measured CLI usage is charged when it is higher than the reservation.

Edit `research/budget.json` and `research/questions.json` to set the project's
limits. The checked-in defaults reserve 9,000 tokens per run with ceilings of
12,000 daily, 70,000 weekly, and 280,000 monthly. These are harness accounting
limits, not provider-side billing controls.

The GitHub workflow uses the official Codex Action in a read-only, key-bearing
job. A separate keyless job publishes the result and ledger to a public review
branch. See [docs/research-harness.md](docs/research-harness.md) for setup,
security boundaries, and the exact accounting model.

On macOS, the reversible [local scheduler](docs/local-scheduling.md) can instead
use an existing Codex login without storing an API key in GitHub.

## Repository map

- `src/agent_design_system/` — format validator, selector engine, enforcer,
  reporters, research planner, and CLI.
- `src/agent_design_system/schemas/` — public JSON Schemas.
- `src/agent_design_system/conformance_suite/` — normative, language-neutral
  behavior vectors shipped with the package.
- `examples/` — one design system plus passing and failing component data.
- `tests/` — format, enforcement, conformance-runner, and budget tests.
- `research/` — queue, budget policy, and generated public state.
- `.github/workflows/` — normal CI and the optional nightly research loop.
- `scripts/` — reversible local scheduler installation and removal.

## Open source

Licensed under Apache-2.0. See [CONTRIBUTING.md](CONTRIBUTING.md),
[SECURITY.md](SECURITY.md), and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
