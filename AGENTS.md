# Agent Design System project guidance

## Goal

Build an open, vendor-neutral design-system format that agents can understand and
independent tools can enforce deterministically. Keep the optional research loop
separate from the enforcement path.

## Invariants

- `adsys check` is read-only. It may report a proposed coercion, but it never
  changes the inspected document.
- `adsys fix` writes to an explicit output path unless `--write` is supplied.
- A rule decision must be deterministic and must end in one of `report`,
  `coerce`, `reject`, or `ignore`.
- Research budget checks happen before an agent process starts. Every attempted
  run is charged at least its reservation, including failed or malformed runs.
- Runtime enforcement has no network or model dependency.
- Keep the core package dependency-free on Python 3.11+.

## Verification

Run before handing off changes:

```sh
python -m unittest discover -s tests -v
python -m agent_design_system validate examples/aurora.design-system.json
python -m agent_design_system conformance
python -m agent_design_system check examples/aurora.design-system.json examples/button.invalid.json --format text
python -m agent_design_system fix examples/aurora.design-system.json examples/button.invalid.json --output /tmp/adsys-button.json
python -m agent_design_system check examples/aurora.design-system.json /tmp/adsys-button.json --format text
```

Review `git diff --check` and the complete diff. Do not claim a scheduled GitHub
workflow has run until its run and resulting branch/PR are verified remotely.
Do not claim the local scheduler is active until `launchctl print` shows the
loaded service.
