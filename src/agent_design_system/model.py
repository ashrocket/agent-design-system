from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["info", "warning", "error"]
Disposition = Literal["report", "coerce", "reject", "ignore"]


@dataclass(frozen=True)
class Token:
    path: str
    value: Any
    token_type: str
    description: str = ""


@dataclass(frozen=True)
class Match:
    path: str
    segments: tuple[str | int, ...]
    value: Any


@dataclass(frozen=True)
class Violation:
    rule_id: str
    path: str
    severity: Severity
    message: str
    original: Any
    disposition: Disposition
    replacement: Any = None
    rationale: str = ""
    line: int | None = None
    column: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "rule_id": self.rule_id,
            "path": self.path,
            "severity": self.severity,
            "message": self.message,
            "original": self.original,
            "disposition": self.disposition,
            "rationale": self.rationale,
        }
        if self.disposition == "coerce":
            data["replacement"] = self.replacement
        if self.line is not None and self.column is not None:
            data["location"] = {"line": self.line, "column": self.column}
        return data


@dataclass
class EnforcementResult:
    source: str
    violations: list[Violation] = field(default_factory=list)
    document: Any = None
    changed: bool = False
    coverage: dict[str, Any] | None = None
    contract_sha256: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts = {"info": 0, "warning": 0, "error": 0}
        for violation in self.violations:
            counts[violation.severity] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        data = {
            "format": "adsf-enforcement-report",
            "version": "0.1.0",
            "source": self.source,
            "changed": self.changed,
            "counts": self.counts,
            "violations": [violation.to_dict() for violation in self.violations],
        }
        if self.coverage is not None:
            data["coverage"] = self.coverage
        if self.contract_sha256 is not None:
            data["contract_sha256"] = self.contract_sha256
        return data
