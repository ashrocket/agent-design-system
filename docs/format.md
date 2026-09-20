# ADSF 0.1 format

ADSF is JSON on purpose: agents can read it directly, JSON Schema can validate
its structure, and independent tools can interpret it without running a model.
The JSON Schema is the structural contract. This document defines the behavior
of the fields the reference enforcer executes.

## Tokens

`tokens` is a tree. A leaf has `$type`, `$value`, and `$description`. Its token
path is the dot-joined sequence of object keys below `tokens`; for example,
`tokens.color.text.primary` declares `{color.text.primary}`. A `$value` may be a
reference to another token. References must exist and may not form cycles.

ADSF 0.1 computes nearest-token coercion for numbers and three- or six-digit hex
colors. Numeric distance is absolute difference. Color distance is Euclidean
distance in sRGB byte space. Equal distances are resolved by token-path lexical
order, making the result stable across implementations.

## Selectors

The selector language is intentionally smaller than JSONPath:

- `$` selects the root.
- `$.components.button.style.color` selects named object keys.
- `*` selects every immediate object value or array item.
- `**` selects the current node and every descendant.
- A decimal segment selects that array index.

Object iteration follows document order, but results must not depend on that
order. Selectors do not contain filters, functions, escaping, or mutation.

## Assertions

An assertion passes or produces exactly one violation per selected value:

| Operator | Meaning |
| --- | --- |
| `token-reference` | The value is an existing `{path}`; optional `category` constrains the path prefix. |
| `enum` | The value occurs in `values`. |
| `equals` | The value equals `value` using JSON equality. |
| `type` | The value has JSON-like type `string`, `number`, `integer`, `boolean`, `object`, `array`, or `null`. |
| `pattern` | The string value matches the regular expression in `value`. |
| `property-exists` | The selected object or array contains the relative dotted path in `value`. |

Selectors only return paths that already exist. To require a potentially missing
field, select its parent and use `property-exists`; for example, select
`$.components.*` and assert `accessibility.name`. Relative property paths support
object keys and decimal array indexes, but not wildcards or escaping in 0.1.

## Decisions

When an assertion fails, branches are evaluated in order. The first matching
`when` chooses its `action`. Every decision array must end with `always`, so a
violation can never fall through.

Actions have fixed meanings:

- `report`: log the violation without proposing a change.
- `coerce`: propose the deterministic replacement described by `strategy`.
- `reject`: log a violation that always fails enforcement regardless of the
  configured severity threshold.
- `ignore`: log the violation but exempt it from failure thresholds. Use this
  only for an explicit, reviewable exception; no wildcard exemption mechanism
  exists in 0.1.

`adsys check` never mutates. `adsys fix` applies only `coerce` actions. If a
coercion strategy cannot calculate a replacement, it degrades to `report`.
Coercion supports `nearest-token` and a literal `replace` value.
All rules inspect the same original document. Fixes are applied afterward in
rule order, so check and fix produce the same violation log; if multiple rules
coerce the same path, the last declared rule supplies the written value.

## Exit behavior and logs

By default, `error` violations fail with exit code 1. `--fail-on` accepts
`info`, `warning`, `error`, or `never`. A `reject` decision always fails. Invalid
input or configuration exits 2. Passing `--log` appends one JSON object per
observed violation; logs never contain model output or hidden state.
