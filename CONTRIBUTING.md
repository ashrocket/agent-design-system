# Contributing

Issues and pull requests are welcome. For behavior changes, add a focused unit
test and state whether the change is backwards-compatible with ADSF 0.1.

Set up an editable checkout and run the suite:

```sh
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
adsys conformance
```

Format proposals should include an example input, the expected violation log,
the selected decision branch, and the expected fixed output. Changes to the JSON
Schema and runtime validator must land together.

By participating, you agree to follow `CODE_OF_CONDUCT.md`. Contributions are
licensed under Apache-2.0.
