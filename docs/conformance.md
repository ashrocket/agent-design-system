# Conformance suite

The JSON Schema proves that an ADSF document has the expected structure. The
conformance suite proves that an enforcer makes the expected decisions.

The canonical manifest and fixtures ship in
`src/agent_design_system/conformance_suite/`. Run all cases with:

```sh
adsys conformance
```

Run one case or request a machine-readable report:

```sh
adsys conformance --case all-decisions-and-coercions
adsys conformance --format json
```

## Manifest contract

The manifest is an `adsf-conformance-suite` JSON document. Each case names a
design-system fixture and one operation:

- `validate` compares whether the system is semantically valid.
- `enforce` checks a document and compares the exact normalized result.

An enforcement expectation contains:

- `changed`, indicating whether fixes changed the output;
- ordered `violations` with rule ID, JSON path, severity, disposition, and an
  optional replacement;
- `fails`, the result at `info`, `warning`, `error`, and `never` thresholds;
- optional `document`, which makes the fixed JSON part of the contract.

Paths are relative to the manifest and may not escape its directory. Case IDs
are unique. The manifest's public JSON Schema is
`src/agent_design_system/schemas/conformance-manifest.schema.json`.

## Independent implementations

A conforming tool does not need to reproduce the reference CLI's prose or
internal architecture. It must produce the same normalized fields for every
normative case. Implementations should run these vectors in their normal test
suite and publish any intentionally unsupported cases rather than silently
changing their meaning.
