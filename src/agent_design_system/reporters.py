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
    if not result.violations:
        return f"PASS {result.source}: no design-system violations"
    lines = [
        f"FOUND {result.source}: {len(result.violations)} violation(s) "
        f"({result.counts['error']} error, {result.counts['warning']} warning, {result.counts['info']} info)"
    ]
    for violation in result.violations:
        suffix = ""
        if violation.replacement is not None:
            suffix = f" -> {json.dumps(violation.replacement, ensure_ascii=False)}"
        lines.append(
            f"{violation.severity.upper():7} {violation.rule_id} {violation.path}: "
            f"{violation.message} [{violation.disposition}]{suffix}"
        )
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
        sarif_results.append(
            {
                "ruleId": violation.rule_id,
                "level": level,
                "message": {"text": violation.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": result.source}
                        },
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
