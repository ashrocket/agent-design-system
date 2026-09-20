from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .enforce import enforce
from .format import FormatError, load_json, validate_design_system
from .reporters import should_fail


THRESHOLDS = ("info", "warning", "error", "never")


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    message: str
    actual: dict[str, Any]
    expected: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "passed": self.passed,
            "message": self.message,
            "actual": self.actual,
            "expected": self.expected,
        }


@dataclass(frozen=True)
class SuiteResult:
    manifest: str
    cases: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "adsf-conformance-report",
            "version": "0.1.0",
            "manifest": self.manifest,
            "passed": self.passed,
            "counts": {
                "total": len(self.cases),
                "passed": sum(case.passed for case in self.cases),
                "failed": sum(not case.passed for case in self.cases),
            },
            "cases": [case.to_dict() for case in self.cases],
        }


def run_suite(
    manifest_path: str | Path, *, only_case: str | None = None
) -> SuiteResult:
    path = Path(manifest_path)
    manifest = load_json(path)
    _validate_manifest(manifest)
    base = path.resolve().parent
    selected = [
        case
        for case in manifest["cases"]
        if only_case is None or case["id"] == only_case
    ]
    if only_case is not None and not selected:
        raise FormatError(f"unknown conformance case: {only_case}")
    results = tuple(_run_case(case, base) for case in selected)
    return SuiteResult(manifest=str(path), cases=results)


def _run_case(case: dict[str, Any], base: Path) -> CaseResult:
    operation = case["operation"]
    expected = case["expected"]
    system = load_json(_resolve_fixture(base, case["system"]))
    actual: dict[str, Any]
    if operation == "validate":
        errors = validate_design_system(system)
        actual = {"valid": not errors}
        return _compare(case["id"], actual, expected)

    document = load_json(_resolve_fixture(base, case["document"]))
    result = enforce(
        system,
        document,
        source=case["document"],
        apply_fixes=bool(case.get("apply_fixes", False)),
    )
    actual = {
        "changed": result.changed,
        "violations": [
            {
                key: value
                for key, value in violation.to_dict().items()
                if key in {"rule_id", "path", "severity", "disposition", "replacement"}
            }
            for violation in result.violations
        ],
        "fails": {
            threshold: should_fail(result, threshold) for threshold in THRESHOLDS
        },
    }
    if "document" in expected:
        actual["document"] = result.document
    return _compare(case["id"], actual, expected)


def _compare(
    case_id: str, actual: dict[str, Any], expected: dict[str, Any]
) -> CaseResult:
    passed = actual == expected
    message = (
        "matches the normative result"
        if passed
        else "result differs from the normative result"
    )
    return CaseResult(case_id, passed, message, actual, expected)


def _resolve_fixture(base: Path, relative: str) -> Path:
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as error:
        raise FormatError(
            f"conformance fixture escapes the suite directory: {relative}"
        ) from error
    return resolved


def _validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise FormatError("conformance manifest root must be an object")
    allowed_manifest = {"$schema", "format", "version", "description", "cases"}
    unknown_manifest = sorted(set(manifest) - allowed_manifest)
    if unknown_manifest:
        raise FormatError(
            f"conformance manifest has unknown property: {unknown_manifest[0]}"
        )
    if manifest.get("format") != "adsf-conformance-suite":
        raise FormatError(
            "conformance manifest format must equal 'adsf-conformance-suite'"
        )
    if not isinstance(manifest.get("version"), str):
        raise FormatError("conformance manifest version must be a string")
    if (
        not isinstance(manifest.get("description"), str)
        or not manifest["description"].strip()
    ):
        raise FormatError("conformance manifest description must be a non-empty string")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FormatError("conformance manifest cases must be a non-empty array")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        prefix = f"conformance cases[{index}]"
        if not isinstance(case, dict):
            raise FormatError(f"{prefix} must be an object")
        allowed_case = {
            "id",
            "operation",
            "system",
            "document",
            "apply_fixes",
            "expected",
        }
        unknown_case = sorted(set(case) - allowed_case)
        if unknown_case:
            raise FormatError(f"{prefix} has unknown property: {unknown_case[0]}")
        for field in ("id", "operation", "system", "expected"):
            if field not in case:
                raise FormatError(f"{prefix} missing required property: {field}")
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id:
            raise FormatError(f"{prefix}.id must be a non-empty string")
        if case_id in seen:
            raise FormatError(f"duplicate conformance case id: {case_id}")
        seen.add(case_id)
        if case["operation"] not in {"validate", "enforce"}:
            raise FormatError(f"{prefix}.operation must be validate or enforce")
        if not isinstance(case["system"], str):
            raise FormatError(f"{prefix}.system must be a path string")
        if not isinstance(case["expected"], dict):
            raise FormatError(f"{prefix}.expected must be an object")
        if "apply_fixes" in case and not isinstance(case["apply_fixes"], bool):
            raise FormatError(f"{prefix}.apply_fixes must be a boolean")
        if case["operation"] == "enforce":
            if not isinstance(case.get("document"), str):
                raise FormatError(f"{prefix}.document is required for enforce")
            _validate_enforcement_expectation(case["expected"], f"{prefix}.expected")
        elif set(case["expected"]) != {"valid"} or not isinstance(
            case["expected"]["valid"], bool
        ):
            raise FormatError(f"{prefix}.expected must contain one boolean valid field")


def _validate_enforcement_expectation(expected: dict[str, Any], path: str) -> None:
    allowed = {"changed", "violations", "fails", "document"}
    if (
        not {"changed", "violations", "fails"}.issubset(expected)
        or set(expected) - allowed
    ):
        raise FormatError(
            f"{path} must contain changed, violations, fails, and optional document"
        )
    if not isinstance(expected["changed"], bool):
        raise FormatError(f"{path}.changed must be a boolean")
    if not isinstance(expected["violations"], list):
        raise FormatError(f"{path}.violations must be an array")
    required_violation = {"rule_id", "path", "severity", "disposition"}
    allowed_violation = required_violation | {"replacement"}
    for index, violation in enumerate(expected["violations"]):
        violation_path = f"{path}.violations[{index}]"
        if (
            not isinstance(violation, dict)
            or not required_violation.issubset(violation)
            or set(violation) - allowed_violation
        ):
            raise FormatError(f"{violation_path} has an invalid shape")
    fails = expected["fails"]
    if (
        not isinstance(fails, dict)
        or set(fails) != set(THRESHOLDS)
        or any(not isinstance(value, bool) for value in fails.values())
    ):
        raise FormatError(
            f"{path}.fails must contain boolean info, warning, error, and never"
        )
