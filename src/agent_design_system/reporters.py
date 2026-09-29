from __future__ import annotations

import json
from typing import Any

from .model import EnforcementResult

SEVERITY_LEVEL = {"never": 99, "error": 3, "warning": 2, "info": 1}


def should_fail(result: EnforcementResult, threshold: str) -> bool:
    if any(violation.disposition == "reject" for violation in result.violations):
        return True
    wanted = SEVERITY_LEVEL[threshold]
    return any(
        violation.disposition != "ignore"
        and SEVERITY_LEVEL[violation.severity] >= wanted
        for violation in result.violations
    )


def render_text(result: EnforcementResult) -> str:
    coverage = ""
    if result.coverage is not None:
        coverage = f"; inspected {result.coverage['declarations']} CSS declaration(s), {result.coverage['governed']} governed"
    if not result.violations:
        return f"PASS {result.source}: no design-system violations{coverage}"
    lines = [
        f"FOUND {result.source}: {len(result.violations)} violation(s) "
        f"({result.counts['error']} error, {result.counts['warning']} warning, {result.counts['info']} info)"
    ]
    for violation in result.violations:
        suffix = ""
        if violation.disposition == "coerce":
            suffix = f" -> {json.dumps(violation.replacement, ensure_ascii=False)}"
        location = (
            f"{result.source}:{violation.line}:{violation.column}"
            if violation.line is not None
            else violation.path
        )
        lines.append(
            f"{violation.severity.upper():7} {violation.rule_id} {location}: "
            f"{violation.message} [{violation.disposition}]{suffix}"
        )
    if coverage:
        lines.append(coverage.lstrip("; "))
    return "\n".join(lines)


def render_json(result: EnforcementResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)


def render_sarif(result: EnforcementResult) -> str:
    rule_ids = sorted({violation.rule_id for violation in result.violations})
    rules = [{"id": rule_id, "name": rule_id} for rule_id in rule_ids]
    sarif_results: list[dict[str, Any]] = []
    for violation in result.violations:
        level = {"error": "error", "warning": "warning", "info": "note"}[
            violation.severity
        ]
        physical: dict[str, Any] = {"artifactLocation": {"uri": result.source}}
        if violation.line is not None and violation.column is not None:
            physical["region"] = {
                "startLine": violation.line,
                "startColumn": violation.column,
            }
        sarif_results.append(
            {
                "ruleId": violation.rule_id,
                "level": level,
                "message": {"text": violation.message},
                "locations": [
                    {
                        "physicalLocation": physical,
                        "logicalLocations": [
                            {"fullyQualifiedName": violation.path, "kind": "property"}
                        ],
                    }
                ],
                "properties": {
                    "jsonPath": violation.path,
                    "disposition": violation.disposition,
                    "replacement": violation.replacement,
                },
            }
        )
    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "columnKind": "unicodeCodePoints",
                "tool": {
                    "driver": {
                        "name": "adsys",
                        "rules": rules,
                    }
                },
                "results": sarif_results,
            }
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def render(result: EnforcementResult, output_format: str) -> str:
    if output_format == "text":
        return render_text(result)
    if output_format == "json":
        return render_json(result)
    if output_format == "sarif":
        return render_sarif(result)
    raise ValueError(f"unsupported report format: {output_format}")
