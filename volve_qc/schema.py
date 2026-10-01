"""Structured QC issue records.

One schema, one shape, every finding. The audit log is the product: it has to
be machine-checkable by someone who did not run the tool.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from ._version import RULESET_VERSION


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


class Correction(str, Enum):
    """What the tool did, not what it recommends."""

    NONE = "none"
    """Flagged only. Raw value preserved byte-for-byte."""

    APPLIED = "applied"
    """A reversible, provably-safe normalisation was written to the output."""

    SUGGESTED = "suggested"
    """A fix is proposed but not applied."""


@dataclass(frozen=True)
class Issue:
    """A single data-quality finding tied to an exact source row."""

    rule_id: str
    """Stable identifier, e.g. ``QC001_NEGATIVE_RATE``. Never renumbered."""

    well_id: str | None
    date: str | None
    """ISO-8601 date, or null when the finding is not date-scoped."""

    severity: Severity
    message: str

    source_row: int
    """1-indexed line in the input file, header included. Traceable by hand."""

    column: str | None = None
    observed: Any = None
    expected: str | None = None
    """Human-readable constraint that was violated."""

    unit: str | None = None
    auto_correctable: bool = False
    suggestion: str | None = None
    correction: Correction = Correction.NONE
    original_value: Any = None
    """Kept even when nothing changed, so the log is self-contained."""

    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["correction"] = self.correction.value
        return d

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=str)


@dataclass
class AuditReport:
    """Run-level provenance plus every issue found."""

    tool: str
    tool_version: str
    input_path: str
    input_sha256: str
    """Content hash. Proves which bytes were audited."""

    run_started: str
    run_finished: str | None = None
    rows_read: int = 0
    wells_seen: int = 0
    ruleset_version: str = RULESET_VERSION
    """Meaning of the findings, not of the tool.

    Quoted in every report so a finding stays interpretable after the ruleset
    moves on. Defaults to the value in ``_version`` rather than a literal, so a
    bump cannot leave this field quietly stale.
    """
    config: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def extend(self, issues: list[Issue]) -> None:
        self.issues.extend(issues)

    def by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.issues:
            out[i.rule_id] = out.get(i.rule_id, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {s.value: 0 for s in Severity}
        for i in self.issues:
            out[i.severity.value] += 1
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "wells_seen": self.wells_seen,
            "issue_count": len(self.issues),
            "by_severity": self.by_severity(),
            "by_rule": self.by_rule(),
            "auto_correctable": sum(1 for i in self.issues if i.auto_correctable),
            "corrections_applied": sum(
                1 for i in self.issues if i.correction is Correction.APPLIED
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "tool_version": self.tool_version,
            "ruleset_version": self.ruleset_version,
            "input": {
                "path": self.input_path,
                "sha256": self.input_sha256,
                "rows_read": self.rows_read,
                "wells_seen": self.wells_seen,
            },
            "run": {"started": self.run_started, "finished": self.run_finished},
            "config": self.config,
            "summary": self.summary(),
            "issues": [i.to_dict() for i in self.issues],
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False, default=str)

    def to_jsonl(self) -> str:
        header = json.dumps(
            {
                "_record": "header",
                "tool": self.tool,
                "tool_version": self.tool_version,
                "ruleset_version": self.ruleset_version,
                "input_path": self.input_path,
                "input_sha256": self.input_sha256,
                "run_started": self.run_started,
                "run_finished": self.run_finished,
                "rows_read": self.rows_read,
                "wells_seen": self.wells_seen,
                "config": self.config,
                "summary": self.summary(),
            },
            sort_keys=True,
            default=str,
        )
        lines = [header]
        lines.extend(i.to_json_line() for i in self.issues)
        return "\n".join(lines)
