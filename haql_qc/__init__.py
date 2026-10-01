"""haql-qc: production data QC and reconciliation with an audit trail."""

from .schema import AuditReport, Correction, Issue, Severity
from .loader import load_production_csv
from .rules import RULES, RULE_META, run_all
from ._version import RULESET_VERSION, TOOL, __version__

TOOL_VERSION = __version__

__all__ = [
    "AuditReport",
    "Correction",
    "Issue",
    "Severity",
    "TOOL",
    "TOOL_VERSION",
    "RULESET_VERSION",
    "load_production_csv",
    "RULES",
    "RULE_META",
    "run_all",
    "__version__",
]
